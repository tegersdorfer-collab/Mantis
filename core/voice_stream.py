"""Pro-Verbindung Session-Manager für /ws/voice/stream: kombiniert Silero-VAD-
Segmentierung mit openWakeWord-Erkennung. Der Router lässt erkannte Sprache durch
den Adress-Check aus core/voice.py laufen, bevor daraus ein Agent-Turn entsteht."""
from __future__ import annotations

import asyncio
import tempfile
import time
import wave
import logging
from collections import deque
from collections.abc import Awaitable, Callable

import config
from core import stt_runtime
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
_DEFAULT_PARTIAL = object()
PARTIAL_SKIP_REASONS = (
    "busy", "budget", "interval", "no_speech", "disabled", "not_wired",
)
log = logging.getLogger(__name__)


async def transcribe_pcm(pcm_audio: bytes) -> tuple[str, bool]:
    """Schreibt PCM als WAV und liefert Text plus nicht-freigebenden Namenshinweis."""
    from core.voice import transcribe_audio_with_hint

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return await transcribe_audio_with_hint(tmp.name)


async def transcribe_pcm_partial(pcm_audio: bytes) -> tuple[str, bool] | None:
    """Versucht einen Teil-STT-Lauf; bei belegtem gemeinsamen STT-Lock wird er verworfen."""
    from core.voice import transcribe_audio_partial_with_hint

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return await transcribe_audio_partial_with_hint(tmp.name)


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
        partial_transcriber: Callable[
            [bytes], Awaitable[str | tuple[str, bool] | None]
        ] | None | object = _DEFAULT_PARTIAL,
        partial_executor_available: Callable[[], bool] | None | object = _DEFAULT_PARTIAL,
        on_partial: Callable[[str], None] | None = None,
        partial_interval_ms: float | None = None,
        clock: Callable[[], float] | None = None,
        bargein_active_fn: Callable[[], bool] | None = None,
        on_bargein: Callable[[float], Awaitable[None]] | None = None,
    ):
        self._segmenter = vad_segmenter
        self._wakeword = wakeword_detector
        self._conversation_active_fn = conversation_active_fn
        self._wakeword_hits: deque[int] = deque()
        self._wakeword_detected = False
        self._wakeword_samples_processed = 0
        self._vad_samples_processed = 0
        # Independent byte-accumulation buffers: the two detectors require
        # different frame sizes (see VAD_FRAME_BYTES / WAKEWORD_FRAME_BYTES
        # above), so incoming raw chunks (arbitrary size, e.g. 8ms from the
        # AudioWorklet) must be buffered separately per detector rather than
        # fed through on a shared per-call cadence.
        self._vad_buffer = bytearray()
        self._wakeword_buffer = bytearray()
        self._partial_transcriber = (
            transcribe_pcm_partial if partial_transcriber is _DEFAULT_PARTIAL
            else partial_transcriber
        )
        self._partial_executor_available = (
            partial_stt_executor_available
            if partial_executor_available is _DEFAULT_PARTIAL
            else partial_executor_available
        )
        self._partial_wired = callable(self._partial_transcriber) and callable(
            self._partial_executor_available
        )
        self._on_partial = on_partial
        self._partial_interval_ms = float(
            config.VOICE_PARTIAL_STT_MS if partial_interval_ms is None else partial_interval_ms
        )
        self._clock = clock or time.monotonic
        self._bargein_active_fn = bargein_active_fn or (lambda: False)
        self._on_bargein = on_bargein
        self._bargein_notified = False
        self._speech_started_at: float | None = None
        self._last_partial_at: float | None = None
        self._partial_task: asyncio.Task | None = None
        self._partial_runs = 0
        self._partial_skip_reasons = {reason: 0 for reason in PARTIAL_SKIP_REASONS}
        self._partial_text = ""
        self._partial_wakeword_hint = False
        self._partial_audio_end_sample_index: int | None = None
        self._partial_run_generation: int | None = None
        self._finalizing = False
        self._closed = False
        self._segment_generation = 0

    def _reset_segment_state(self) -> None:
        self._segment_generation += 1
        self._bargein_notified = False
        self._speech_started_at = None
        self._last_partial_at = None
        self._partial_runs = 0
        self._partial_skip_reasons = {reason: 0 for reason in PARTIAL_SKIP_REASONS}
        self._partial_text = ""
        self._partial_wakeword_hint = False
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
            self._wakeword_samples_processed += len(frame) // 2
            detected = bool(self._wakeword.check(frame))
            if detected and not self._wakeword_detected:
                self._wakeword_hits.append(self._wakeword_samples_processed)
            self._wakeword_detected = detected

        result = None
        while len(self._vad_buffer) >= VAD_FRAME_BYTES:
            frame = bytes(self._vad_buffer[:VAD_FRAME_BYTES])
            del self._vad_buffer[:VAD_FRAME_BYTES]

            was_speaking = getattr(self._segmenter, "is_speaking", None) is True
            segment = self._segmenter.process_chunk(frame)
            self._vad_samples_processed += len(frame) // 2
            await self._maybe_trigger_bargein()
            if segment is None:
                if (
                    was_speaking
                    and getattr(self._segmenter, "is_speaking", None) is not True
                ):
                    self._discard_wakeword_hits_through(self._vad_samples_processed)
                    self._reset_segment_state()
                else:
                    self._maybe_start_partial()
                continue

            segment_end_sample = self._vad_samples_processed
            segment_start_sample = max(
                0, segment_end_sample - len(segment.audio) // 2,
            )
            wake_fired = self._wakeword_fired_for_segment(
                segment_start_sample, segment_end_sample,
            )

            if not wake_fired and not self._conversation_active_fn():
                self._reset_segment_state()
                continue

            partial_runs = self._partial_runs
            partial_skip_reasons = dict(self._partial_skip_reasons)
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

            wakeword_hint = False
            if not text:
                transcript = await transcribe_pcm(segment.audio)
                if isinstance(transcript, tuple) and len(transcript) == 2:
                    text, wakeword_hint = transcript
                else:
                    text = transcript
                    from core.voice import has_voice_wakeword_hint

                    wakeword_hint = has_voice_wakeword_hint(str(text or ""))
            elif partial_as_final:
                from core.voice import has_voice_wakeword_hint

                wakeword_hint = bool(
                    self._partial_wakeword_hint
                    or has_voice_wakeword_hint(str(text or ""))
                )
            partial_text = self._partial_text or partial_text
            stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
            self._reset_segment_state()
            result = {
                "text": text,
                "addressed": True,
                "wakeword_triggered": wake_fired,
                "wakeword_hint": bool(wakeword_hint),
                "stt_ms": stt_ms,
                "turn_started_at": turn_started_at,
                "partial_runs": partial_runs,
                "partial_skip_reasons": partial_skip_reasons,
                "partial_text": partial_text,
                "partial_as_final": partial_as_final,
            }

        return result

    def _discard_wakeword_hits_through(self, sample_index: int) -> None:
        while self._wakeword_hits and self._wakeword_hits[0] <= sample_index:
            self._wakeword_hits.popleft()
        # _wakeword_detected bleibt: Ein anhaltendes Detektorsignal ist keine neue Flanke.

    def _wakeword_fired_for_segment(self, start_sample: int, end_sample: int) -> bool:
        preroll_samples = int(
            max(0, float(getattr(config, "VOICE_WAKE_PREROLL_MS", 1500)))
            * SAMPLE_RATE / 1000
        )
        earliest_sample = max(0, start_sample - preroll_samples)
        while self._wakeword_hits and self._wakeword_hits[0] < earliest_sample:
            self._wakeword_hits.popleft()
        fired = any(
            earliest_sample <= hit <= end_sample
            for hit in self._wakeword_hits
        )
        self._discard_wakeword_hits_through(end_sample)
        return fired

    async def _maybe_trigger_bargein(self) -> None:
        if (
            self._bargein_notified
            or self._on_bargein is None
            or not getattr(config, "VOICE_BARGEIN", True)
            or not self._bargein_active_fn()
            or getattr(self._segmenter, "is_speaking", None) is not True
            or getattr(self._segmenter, "current_chunk_is_speech", None) is not True
        ):
            return
        speech_ms = float(getattr(self._segmenter, "current_speech_ms", 0.0) or 0.0)
        rms = float(getattr(self._segmenter, "current_speech_rms", 0.0) or 0.0)
        if speech_ms < float(getattr(config, "VOICE_BARGEIN_MIN_SPEECH_MS", 300)):
            return
        if rms < float(getattr(config, "VOICE_BARGEIN_MIN_RMS", 800)):
            return

        self._bargein_notified = True
        await self._on_bargein(self._clock())

    def _maybe_start_partial(self) -> None:
        if self._closed:
            return
        speaking = getattr(self._segmenter, "is_speaking", None) is True
        current_chunk_is_speech = (
            getattr(self._segmenter, "current_chunk_is_speech", None) is True
        )
        if not speaking or not current_chunk_is_speech:
            self._partial_skip_reasons["no_speech"] += 1
            return
        if self._partial_interval_ms <= 0:
            self._partial_skip_reasons["disabled"] += 1
            return
        if not self._partial_wired:
            self._partial_skip_reasons["not_wired"] += 1
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
            self._partial_skip_reasons["interval"] += 1
            return
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_skip_reasons["busy"] += 1
            return
        self._last_partial_at = now
        if self._partial_task is not None:
            try:
                self._partial_task.result()
            except Exception as exc:
                log.debug("Teil-STT fehlgeschlagen (%s)", type(exc).__name__)
            self._partial_task = None
        try:
            if not self._partial_executor_available():
                self._partial_skip_reasons["busy"] += 1
                return
        except Exception as exc:
            log.debug("STT-Executor-Status nicht verfügbar (%s)", type(exc).__name__)
            self._partial_skip_reasons["not_wired"] += 1
            return

        current_audio = getattr(self._segmenter, "current_audio", None)
        if not isinstance(current_audio, (bytes, bytearray)) or not current_audio:
            self._partial_skip_reasons["no_speech"] += 1
            return
        audio_seconds = len(current_audio) / (2 * SAMPLE_RATE)
        predicted_ms = stt_runtime.estimate_stt_runtime_ms(audio_seconds)
        if predicted_ms > float(getattr(config, "VOICE_PARTIAL_MAX_MS", 600)):
            self._partial_skip_reasons["budget"] += 1
            return
        generation = self._segment_generation
        audio = bytes(current_audio)
        audio_end_sample_index = len(audio) // 2
        self._partial_audio_end_sample_index = audio_end_sample_index
        self._partial_run_generation = generation
        self._partial_task = asyncio.create_task(
            self._run_partial(audio, generation, audio_end_sample_index),
            name="voice-partial-stt",
        )
        self._partial_runs += 1

    async def _run_partial(
        self, audio: bytes, generation: int, audio_end_sample_index: int,
    ) -> str | None:
        try:
            transcript = await self._partial_transcriber(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Teil-STT fehlgeschlagen (%s)", type(exc).__name__)
            return None

        if transcript is None:
            return None
        if isinstance(transcript, tuple) and len(transcript) == 2:
            text, wakeword_hint = transcript
        else:
            text = transcript
            from core.voice import has_voice_wakeword_hint

            wakeword_hint = has_voice_wakeword_hint(str(text or ""))
        if text is None:
            return None
        text = str(text).strip()
        if not text:
            return None
        if generation == self._segment_generation and not self._closed and (
            getattr(self._segmenter, "is_speaking", None) is True or self._finalizing
        ):
            self._partial_text = text
            self._partial_wakeword_hint = bool(wakeword_hint)
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
        self._wakeword_hits.clear()
        self._wakeword_detected = False
        self._partial_text = ""
        self._partial_audio_end_sample_index = None
        self._partial_run_generation = None
        self._vad_buffer.clear()
        self._wakeword_buffer.clear()
        self._reset_segment_state()
