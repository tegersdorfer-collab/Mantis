"""Pro-Verbindung Session-Manager für /ws/voice/stream: kombiniert Silero-VAD-
Segmentierung mit openWakeWord-Erkennung. Ersetzt für den WebSocket-Pfad sowohl
die client-seitige RMS-Erkennung als auch den LLM-basierten
is_addressed_to_mantis()-Text-Check aus core/voice.py (siehe
docs/superpowers/specs/2026-07-05-vad-wakeword-streaming-design.md)."""
from __future__ import annotations

import tempfile
import time
import wave
from typing import Callable

from core.vad import VadSegmenter

SAMPLE_RATE = 16000

# Silero VAD's ONNX export requires exactly 512 samples (32ms @ 16kHz) per
# speech_probability() call. openWakeWord's documented standard chunk size is
# 1280 samples (80ms @ 16kHz) — see openwakeword.model.Model.predict docstring
# ("Ideally should be multiples of 80 ms (1280 samples)"). The AudioWorklet in
# apps/desktop/public/pcm-worklet.js posts small raw frames (128 samples/8ms)
# with no accumulation, so VoiceStreamSession buffers incoming bytes
# independently per detector before dispatching full-size frames.
VAD_FRAME_BYTES = 512 * 2  # 1024 bytes
WAKEWORD_FRAME_BYTES = 1280 * 2  # 2560 bytes


async def transcribe_pcm(pcm_audio: bytes) -> str:
    """Schreibt rohes PCM als WAV und transkribiert über die bestehende
    Whisper-Pipeline (core.voice.transcribe_audio erwartet einen Dateipfad)."""
    from core.voice import transcribe_audio

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return await transcribe_audio(tmp.name)


class VoiceStreamSession:
    def __init__(
        self,
        vad_segmenter: VadSegmenter,
        wakeword_detector,
        conversation_active_fn: Callable[[], bool],
    ):
        self._segmenter = vad_segmenter
        self._wakeword = wakeword_detector
        self._conversation_active_fn = conversation_active_fn
        self._wake_fired_in_segment = False
        # Independent byte-accumulation buffers: the two detectors require
        # different frame sizes (see VAD_FRAME_BYTES / WAKEWORD_FRAME_BYTES
        # above), so incoming raw chunks (arbitrary size, e.g. 8ms from the
        # AudioWorklet) must be buffered separately per detector rather than
        # fed through on a shared per-call cadence.
        self._vad_buffer = bytearray()
        self._wakeword_buffer = bytearray()

    async def handle_chunk(self, pcm_chunk: bytes, muted: bool) -> dict | None:
        if muted:
            return None

        self._vad_buffer.extend(pcm_chunk)
        self._wakeword_buffer.extend(pcm_chunk)

        while len(self._wakeword_buffer) >= WAKEWORD_FRAME_BYTES:
            frame = bytes(self._wakeword_buffer[:WAKEWORD_FRAME_BYTES])
            del self._wakeword_buffer[:WAKEWORD_FRAME_BYTES]
            if self._wakeword.check(frame):
                self._wake_fired_in_segment = True

        result = None
        while len(self._vad_buffer) >= VAD_FRAME_BYTES:
            frame = bytes(self._vad_buffer[:VAD_FRAME_BYTES])
            del self._vad_buffer[:VAD_FRAME_BYTES]

            segment = self._segmenter.process_chunk(frame)
            if segment is None:
                continue

            wake_fired = self._wake_fired_in_segment
            self._wake_fired_in_segment = False

            if not wake_fired and not self._conversation_active_fn():
                continue

            stt_started = time.perf_counter()
            text = await transcribe_pcm(segment.audio)
            stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
            result = {"text": text, "addressed": True, "stt_ms": stt_ms}

        return result
