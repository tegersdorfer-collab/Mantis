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
from collections import deque
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
        self._partial_audio_end_sample_index: int | None = None
        self._partial_run_generation: int | None = None
        self._partial_ms_per_audio_second: deque[float] = deque(maxlen=5)
        self._finalizing = False
        self._closed = False
        self._segment_generation = 0

    def _reset_segment_state(self) -> None:
        self._segment_generation += 1
        self._speech_started_at = None
        self._last_partial_at = None
        self._partial_runs = 0
        self._partial_text = ""
        self._partial_audio_end_sample_index = None
        self._partial_run_generation = None
        setter = getattr(self._segmenter, "set_partial_transcript", None)
        if callable(setter):
            setter("")

    async def handle_chunk(self, pcm_chunk: bytes, muted: bool) -> dict | None:
        if muted or self._closed:
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
            turn_started_at = time.perf_counter()
            stt_started = turn_started_at
            partial_text = self._partial_text
            last_speech_sample_index = getattr(segment, "last_speech_sample_index", None)
            if last_speech_sample_index is None:
                last_speech_sample_index = len(segment.audio) // 2
            task = self._partial_task
            partial_covers_speech = (
                task is not None
                and self._partial_run_generation == self._segment_generation
                and self._partial_audio_end_sample_index is not None
                and self._partial_audio_end_sample_index >= last_speech_sample_index
            )
            text = None
            partial_as_final = False
            if partial_covers_speech:
                self._finalizing = True
                try:
                    text = await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("Teil-STT-Ergebnis konnte nicht final genutzt werden (%s)", type(exc).__name__)
                finally:
                    self._finalizing = False
                if text:
                    text = str(text).strip()
                    partial_text = text
                    partial_as_final = bool(text)

            if not text:
                text = await transcribe_pcm(segment.audio)
            partial_text = self._partial_text or partial_text
            stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
            self._reset_segment_state()
            result = {
                "text": text,
                "addressed": True,
                "stt_ms": stt_ms,
                "turn_started_at": turn_started_at,
                "partial_runs": partial_runs,
                "partial_text": partial_text,
                "partial_as_final": partial_as_final,
            }

        return result

    def _maybe_start_partial(self) -> None:
        if self._closed:
            return
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
            self._partial_audio_end_sample_index = None
            self._partial_run_generation = None
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
        average_ms_per_audio_second = (
            sum(self._partial_ms_per_audio_second) / len(self._partial_ms_per_audio_second)
            if self._partial_ms_per_audio_second else None
        )
        if average_ms_per_audio_second is not None:
            audio_seconds = len(current_audio) / (2 * SAMPLE_RATE)
            predicted_ms = average_ms_per_audio_second * audio_seconds
            if predicted_ms > float(getattr(config, "VOICE_PARTIAL_MAX_MS", 600)):
                return
        generation = self._segment_generation
        audio = bytes(current_audio)
        audio_end_sample_index = len(audio) // 2
        self._partial_audio_end_sample_index = audio_end_sample_index
        self._partial_run_generation = generation
        self._last_partial_at = now
        self._partial_task = asyncio.create_task(
            self._run_partial(audio, generation, audio_end_sample_index),
            name="voice-partial-stt",
        )
        self._partial_runs += 1

    async def _run_partial(
        self, audio: bytes, generation: int, audio_end_sample_index: int,
    ) -> str | None:
        started_at = self._clock()
        try:
            text = await self._partial_transcriber(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Teil-STT fehlgeschlagen (%s)", type(exc).__name__)
            return None
        finally:
            audio_seconds = len(audio) / (2 * SAMPLE_RATE)
            elapsed_ms = max(0.0, (self._clock() - started_at) * 1000)
            if audio_seconds > 0:
                self._partial_ms_per_audio_second.append(elapsed_ms / audio_seconds)

        if text is None:
            return None
        text = str(text).strip()
        if not text:
            return None
        if generation == self._segment_generation and not self._closed and (
            getattr(self._segmenter, "is_speaking", None) is True or self._finalizing
        ):
            self._partial_text = text
            setter = getattr(self._segmenter, "set_partial_transcript", None)
            if callable(setter):
                try:
                    setter(text, audio_end_sample_index=audio_end_sample_index)
                except TypeError:
                    setter(text)
            if self._on_partial is not None:
                try:
                    self._on_partial(text)
                except Exception as exc:
                    log.debug("Voice-Vorarbeit konnte nicht starten (%s)", type(exc).__name__)
        return text

    async def close(self) -> None:
        """Stoppt Callbacks und räumt die laufende Teil-STT-Task der Session ab."""
        if self._closed:
            return
        self._closed = True
        self._on_partial = None
        self._segment_generation += 1
        task = self._partial_task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._partial_task = None
        self._partial_text = ""
        self._partial_audio_end_sample_index = None
        self._partial_run_generation = None
        self._vad_buffer.clear()
        self._wakeword_buffer.clear()
        self._reset_segment_state()
