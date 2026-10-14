"""Pro-Verbindung Session-Manager für /ws/voice/stream: kombiniert Silero-VAD-
Segmentierung mit openWakeWord-Erkennung. Ersetzt für den WebSocket-Pfad sowohl
die client-seitige RMS-Erkennung als auch den LLM-basierten
is_addressed_to_mantis()-Text-Check aus core/voice.py (siehe
docs/superpowers/specs/2026-07-05-vad-wakeword-streaming-design.md)."""
from __future__ import annotations

import asyncio
import tempfile
import time
import wave
import logging
from collections.abc import Awaitable, Callable

import config
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
log = logging.getLogger(__name__)


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


async def transcribe_pcm_partial(pcm_audio: bytes) -> str | None:
    """Versucht einen Teil-STT-Lauf; bei belegtem gemeinsamen STT-Lock wird er verworfen."""
    from core.voice import transcribe_audio_partial

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return await transcribe_audio_partial(tmp.name)


def partial_stt_executor_available() -> bool:
    from core.voice import partial_stt_executor_available as is_available

    return is_available()


class VoiceStreamSession:
    def __init__(
        self,
        vad_segmenter: VadSegmenter,
        wakeword_detector,
        conversation_active_fn: Callable[[], bool],
        *,
        partial_transcriber: Callable[[bytes], Awaitable[str | None]] | None = None,
        partial_executor_available: Callable[[], bool] | None = None,
        on_partial: Callable[[str], None] | None = None,
        partial_interval_ms: float | None = None,
        clock: Callable[[], float] | None = None,
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
        self._partial_transcriber = partial_transcriber or transcribe_pcm_partial
        self._partial_executor_available = partial_executor_available or partial_stt_executor_available
        self._on_partial = on_partial
        self._partial_interval_ms = float(
            config.VOICE_PARTIAL_STT_MS if partial_interval_ms is None else partial_interval_ms
        )
        self._clock = clock or time.monotonic
        self._speech_started_at: float | None = None
        self._last_partial_at: float | None = None
        self._partial_task: asyncio.Task | None = None
        self._partial_runs = 0
        self._partial_text = ""
        self._segment_generation = 0

    def _reset_segment_state(self) -> None:
        self._segment_generation += 1
        self._speech_started_at = None
        self._last_partial_at = None
        setter = getattr(self._segmenter, "set_partial_transcript", None)
        if callable(setter):
            setter("")

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
                self._maybe_start_partial()
                continue

            wake_fired = self._wake_fired_in_segment
            self._wake_fired_in_segment = False

            if not wake_fired and not self._conversation_active_fn():
                self._reset_segment_state()
                continue

            partial_runs = self._partial_runs
            partial_text = self._partial_text
            self._reset_segment_state()
            turn_started_at = time.perf_counter()
            stt_started = turn_started_at
            text = await transcribe_pcm(segment.audio)
            stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
            result = {
                "text": text,
                "addressed": True,
                "stt_ms": stt_ms,
                "turn_started_at": turn_started_at,
                "partial_runs": partial_runs,
                "partial_text": partial_text,
            }

        return result

    def _maybe_start_partial(self) -> None:
        if getattr(self._segmenter, "is_speaking", None) is not True:
            return
        if getattr(self._segmenter, "current_chunk_is_speech", None) is not True:
            return
        if not (self._wake_fired_in_segment or self._conversation_active_fn()):
            return

        now = self._clock()
        if self._speech_started_at is None:
            self._speech_started_at = now
            self._last_partial_at = now
            self._partial_runs = 0
            self._partial_text = ""
            self._segment_generation += 1

        if now - self._last_partial_at < self._partial_interval_ms / 1000:
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        if self._partial_task is not None:
            try:
                self._partial_task.result()
            except Exception as exc:
                log.debug("Teil-STT fehlgeschlagen (%s)", type(exc).__name__)
            self._partial_task = None
        try:
            if not self._partial_executor_available():
                return
        except Exception as exc:
            log.debug("STT-Executor-Status nicht verfügbar (%s)", type(exc).__name__)
            return

        current_audio = getattr(self._segmenter, "current_audio", None)
        if not isinstance(current_audio, (bytes, bytearray)) or not current_audio:
            return
        self._last_partial_at = now
        self._partial_runs += 1
        generation = self._segment_generation
        self._partial_task = asyncio.create_task(
            self._run_partial(bytes(current_audio), generation),
            name="voice-partial-stt",
        )

    async def _run_partial(self, audio: bytes, generation: int) -> None:
        try:
            text = await self._partial_transcriber(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Teil-STT fehlgeschlagen (%s)", type(exc).__name__)
            return
        if text is None or generation != self._segment_generation:
            return
        if getattr(self._segmenter, "is_speaking", None) is not True:
            return
        text = str(text).strip()
        if not text:
            return
        self._partial_text = text
        setter = getattr(self._segmenter, "set_partial_transcript", None)
        if callable(setter):
            setter(text)
        if self._on_partial is not None:
            try:
                self._on_partial(text)
            except Exception as exc:
                log.debug("Voice-Vorarbeit konnte nicht starten (%s)", type(exc).__name__)
