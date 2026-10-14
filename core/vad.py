"""Silero VAD — Sprachaktivitätserkennung server-seitig, ersetzt die frühere
RMS-Schwelle in apps/desktop/src/voice-capture.ts (siehe
docs/superpowers/specs/2026-07-05-vad-wakeword-streaming-design.md)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import config

SAMPLE_RATE = 16000
MIN_SEGMENT_MS = 300
PREROLL_MS = 400

# Silero VAD's ONNX export requires exactly this many samples per inference
# call (32ms @ 16kHz) — confirmed empirically against the downloaded model.
VAD_REQUIRED_SAMPLES = 512


def _load_session(model_path: Path):
    import onnxruntime as ort
    return ort.InferenceSession(str(model_path))


class SileroVAD:
    """Wraps the Silero VAD ONNX model. The public (recent) export exposes a
    single recurrent `state` tensor of shape (2, batch, 128) rather than the
    separate LSTM `h`/`c` tensors used by older exports — verified empirically
    via `s.get_inputs()` / `s.get_outputs()` on the downloaded model:
      inputs:  input [None, None] float32, state [2, None, 128] float32, sr [] int64
      outputs: output [None, 1] float32, stateN [None, None, None] float32

    CONTEXT PREPENDING: Silero VAD v5+'s ONNX graph expects the last 64 samples
    of the PREVIOUS chunk prepended to the current 512-sample chunk (making the
    actual "input" tensor 576 samples) — this is separate from the recurrent
    `state` tensor and is NOT documented in the graph's input shape metadata
    (which just shows `[None, None]`). Confirmed against the official
    `silero-vad` PyPI package's `OnnxWrapper.__call__` (`context_size = 64` for
    16kHz, `x = torch.cat([self._context, x], dim=1)`). Without this, the model
    silently returns near-zero speech probability for real audio containing
    genuine speech — found via a live end-to-end test that fed real recordings
    through the whole VAD+wake-word pipeline (docs/superpowers/plans/2026-07-05-
    vad-wakeword-streaming.md, Task 6) after the original Task 1 implementation
    only ever exercised this class against a mocked ONNX session."""

    CONTEXT_SAMPLES = 64

    def __init__(self, model_path: Path):
        self._session = _load_session(model_path)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT_SAMPLES, dtype=np.float32)

    def speech_probability(self, pcm_chunk: bytes) -> float:
        n_samples = len(pcm_chunk) // 2
        if n_samples != VAD_REQUIRED_SAMPLES:
            raise ValueError(
                f"SileroVAD.speech_probability requires exactly "
                f"{VAD_REQUIRED_SAMPLES} samples ({VAD_REQUIRED_SAMPLES * 2} bytes), "
                f"got {n_samples} samples ({len(pcm_chunk)} bytes). Callers must "
                f"buffer raw audio to this frame size before calling (see "
                f"core/voice_stream.py::VoiceStreamSession)."
            )
        samples = struct.unpack(f"<{n_samples}h", pcm_chunk)
        audio = np.array(samples, dtype=np.float32) / 32768.0
        audio_with_context = np.concatenate([self._context, audio])
        inputs = {
            "input": audio_with_context[np.newaxis, :],
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            "state": self._state,
        }
        outputs = self._session.run(None, inputs)
        if len(outputs) > 1:
            self._state = np.array(outputs[1], dtype=np.float32)
        self._context = audio_with_context[-self.CONTEXT_SAMPLES:]
        return float(np.array(outputs[0]).flatten()[0])


@dataclass
class SegmentEvent:
    audio: bytes
    duration_ms: float


class VadSegmenter:
    """Zustandsautomat: sammelt PCM-Chunks, erkennt Sprechbeginn/-ende per VAD-
    Wahrscheinlichkeit, liefert fertige Segmente (inkl. Preroll) zurück. Portierte
    Logik von apps/desktop/src/voice-capture.ts's alter tick()-Funktion.

    Bleibt bewusst agnostisch gegenüber der konkreten chunk_ms-Größe: der Aufrufer
    ist dafür verantwortlich, process_chunk() mit Chunks der angegebenen Dauer zu
    füttern. Der WebSocket-Pfad (web/routers/voice.py) puffert rohe, sehr kleine
    AudioWorklet-Frames serverseitig in core/voice_stream.py::VoiceStreamSession
    zu exakt 512-Sample-Blöcken (32ms @ 16kHz, das von Silero VAD geforderte
    Modell-Input-Format) und ruft process_chunk() entsprechend mit chunk_ms=32
    auf — NICHT mit den früher angenommenen 100ms."""

    SPEECH_THRESHOLD = 0.5

    def __init__(self, vad_model: SileroVAD, chunk_ms: float):
        self._vad = vad_model
        self._chunk_ms = chunk_ms
        self._speaking = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._buffer: list[bytes] = []
        self._preroll: list[bytes] = []

    def process_chunk(self, pcm_chunk: bytes) -> SegmentEvent | None:
        prob = self._vad.speech_probability(pcm_chunk)
        is_speech = prob >= self.SPEECH_THRESHOLD

        if is_speech:
            self._silence_ms = 0.0
            if not self._speaking:
                self._speaking = True
                self._speech_ms = 0.0
                self._buffer = list(self._preroll)
            self._speech_ms += self._chunk_ms
            self._buffer.append(pcm_chunk)
        elif self._speaking:
            self._buffer.append(pcm_chunk)
            self._silence_ms += self._chunk_ms
            if self._silence_ms >= config.VOICE_SILENCE_MS:
                self._speaking = False
                total_ms = self._speech_ms + self._silence_ms
                segment = b"".join(self._buffer)
                self._buffer = []
                if self._speech_ms >= MIN_SEGMENT_MS:
                    return SegmentEvent(audio=segment, duration_ms=total_ms)
                return None

        preroll_chunks = max(1, int(PREROLL_MS / self._chunk_ms))
        self._preroll.append(pcm_chunk)
        self._preroll = self._preroll[-preroll_chunks:]
        return None
