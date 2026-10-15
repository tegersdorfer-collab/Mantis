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
from core.vad import VadSegmenter, classify_utterance
from core.voice_segments import record_voice_segment
from core.voice_text import normalize_wakeword_transcript

SAMPLE_RATE = 16000
WAKEWORD_SAFETY_MARGIN_MS = 150

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
        return await transcribe_audio_with_hint(tmp.name, wakeword_triggered=False)


async def transcribe_pcm_partial(pcm_audio: bytes) -> tuple[str, bool] | None:
    """Versucht einen Teil-STT-Lauf; bei belegtem gemeinsamen STT-Lock wird er verworfen."""
    from core.voice import transcribe_audio_partial_with_hint

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        with wave.open(tmp.name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return await transcribe_audio_partial_with_hint(
            tmp.name, wakeword_triggered=False,
        )


def crop_wakeword_audio(
    pcm_audio: bytes,
    *,
    segment_start_sample: int,
    wakeword_hit_sample: int | None,
) -> tuple[bytes, int]:
    """Schneidet konservativ vor dem ersten Trefferframe; ohne Treffer bleibt Audio."""
    segment_start = max(0, int(segment_start_sample))
    if wakeword_hit_sample is None:
        return bytes(pcm_audio), segment_start
    safety_samples = int(SAMPLE_RATE * WAKEWORD_SAFETY_MARGIN_MS / 1000)
    offset_ms = float(getattr(config, "VOICE_WAKE_CROP_OFFSET_MS", 0))
    offset_samples = int(SAMPLE_RATE * offset_ms / 1000)
    crop_start = max(
        segment_start,
        int(wakeword_hit_sample) + offset_samples - safety_samples,
    )
    offset_samples = crop_start - segment_start
    return bytes(pcm_audio[offset_samples * 2:]), crop_start


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
        on_speech_progress: Callable[[bytes, float, int], None] | None = None,
        smart_turn_analyzer=None,
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
        self._on_speech_progress = on_speech_progress
        self._bargein_notified = False
        self._segment_overlapped = False
        self._speaker_early_progress_sent = False
        self._speaker_final_progress_sent = False
        self._speech_started_at: float | None = None
        self._last_partial_at: float | None = None
        self._partial_task: asyncio.Task | None = None
        self._partial_runs = 0
        self._partial_skip_reasons = {reason: 0 for reason in PARTIAL_SKIP_REASONS}
        self._partial_text = ""
        self._partial_raw_text = ""
        self._partial_heuristic_text = ""
        self._partial_wakeword_hint = False
        self._partial_transcripts: list[dict] = []
        self._partial_audio_end_sample_index: int | None = None
        self._partial_audio_start_sample_index: int | None = None
        self._partial_wakeword_fired = False
        self._partial_run_generation: int | None = None
        self._finalizing = False
        self._closed = False
        self._segment_generation = 0
        self._smart_turn_enabled = bool(getattr(config, "VOICE_SMART_TURN", True))
        if smart_turn_analyzer is None and self._smart_turn_enabled:
            from core.smart_turn import get_smart_turn_analyzer

            smart_turn_analyzer = get_smart_turn_analyzer()
        self._smart_turn = smart_turn_analyzer
        self._smart_turn_task: asyncio.Task | None = None
        self._smart_turn_warmup_task: asyncio.Task | None = None
        self._smart_turn_warmup_started = False
        self._smart_turn_task_generation: int | None = None
        self._smart_turn_check_silence_ms: float | None = None
        self._next_smart_turn_check_ms: float | None = None
        self._turn_model_p: float | None = None
        self._turn_model_ms = 0.0
        self._smart_turn_failed = False
        self._turn_decision = "heuristic" if not self._smart_turn_enabled else None

    def _reset_segment_state(self) -> None:
        self._segment_generation += 1
        self._cancel_smart_turn_task()
        self._smart_turn_check_silence_ms = None
        self._next_smart_turn_check_ms = None
        self._turn_model_p = None
        self._turn_model_ms = 0.0
        self._turn_decision = (
            "fallback" if self._smart_turn_failed
            else "heuristic" if not self._smart_turn_enabled else None
        )
        if self._smart_turn_failed:
            self._set_smart_turn_mode("fallback")
        self._bargein_notified = False
        self._segment_overlapped = False
        self._speaker_early_progress_sent = False
        self._speaker_final_progress_sent = False
        self._speech_started_at = None
        self._last_partial_at = None
        self._partial_runs = 0
        self._partial_skip_reasons = {reason: 0 for reason in PARTIAL_SKIP_REASONS}
        self._partial_text = ""
        self._partial_raw_text = ""
        self._partial_heuristic_text = ""
        self._partial_wakeword_hint = False
        self._partial_transcripts = []
        self._partial_audio_end_sample_index = None
        self._partial_audio_start_sample_index = None
        self._partial_wakeword_fired = False
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
                hit_sample = getattr(
                    self._wakeword, "first_positive_frame_end_sample", None,
                )
                try:
                    hit_sample = int(hit_sample)
                except (TypeError, ValueError):
                    hit_sample = self._wakeword_samples_processed
                if not 0 <= hit_sample <= self._wakeword_samples_processed:
                    hit_sample = self._wakeword_samples_processed
                self._wakeword_hits.append(hit_sample)
            self._wakeword_detected = detected

        result = None
        while len(self._vad_buffer) >= VAD_FRAME_BYTES:
            frame = bytes(self._vad_buffer[:VAD_FRAME_BYTES])
            del self._vad_buffer[:VAD_FRAME_BYTES]

            self._collect_smart_turn_result()
            was_speaking = getattr(self._segmenter, "is_speaking", None) is True
            segment = self._segmenter.process_chunk(frame)
            self._vad_samples_processed += len(frame) // 2
            await self._maybe_trigger_bargein()
            if (
                callable(self._on_speech_progress)
                and self._bargein_active_fn()
                and getattr(self._segmenter, "is_speaking", None) is True
                and getattr(self._segmenter, "current_chunk_is_speech", None) is True
            ):
                self._segment_overlapped = True
                speech_ms = float(
                    getattr(self._segmenter, "current_speech_ms", 0.0) or 0.0,
                )
                early_due = speech_ms >= 600 and not self._speaker_early_progress_sent
                final_due = speech_ms >= 1_000 and not self._speaker_final_progress_sent
                if early_due:
                    self._speaker_early_progress_sent = True
                if final_due:
                    self._speaker_final_progress_sent = True
                if (
                    bool(getattr(config, "VOICE_BARGEIN", True))
                    and (early_due or final_due)
                ):
                    current_audio = getattr(
                        self._segmenter,
                        "current_speech_audio",
                        getattr(self._segmenter, "current_audio", b""),
                    )
                    if not isinstance(current_audio, (bytes, bytearray)):
                        current_audio = getattr(self._segmenter, "current_audio", b"")
                    if isinstance(current_audio, (bytes, bytearray)) and current_audio:
                        self._on_speech_progress(
                            bytes(current_audio), speech_ms, self._segment_generation,
                        )
            if segment is None:
                if (
                    was_speaking
                    and getattr(self._segmenter, "is_speaking", None) is not True
                ):
                    self._discard_wakeword_hits_through(self._vad_samples_processed)
                    self._reset_segment_state()
                elif getattr(self._segmenter, "current_chunk_is_speech", None) is True:
                    self._reset_smart_turn_for_resumed_speech()
                    self._maybe_start_smart_turn_warmup()
                    self._maybe_start_partial()
                else:
                    self._maybe_start_smart_turn()
                    self._maybe_start_partial()
                continue

            self._collect_smart_turn_result()

            segment_end_sample = self._vad_samples_processed
            segment_start_sample = max(
                0, segment_end_sample - len(segment.audio) // 2,
            )
            wakeword_hit_sample = self._wakeword_hit_for_range(
                segment_start_sample, segment_end_sample,
                consume=True,
            )
            wake_fired = wakeword_hit_sample is not None
            stt_audio, crop_start_sample = crop_wakeword_audio(
                segment.audio,
                segment_start_sample=segment_start_sample,
                wakeword_hit_sample=wakeword_hit_sample,
            )

            turn_model_p = self._turn_model_p
            turn_model_ms = round(self._turn_model_ms, 1)
            turn_decision = self._turn_decision or "fallback"
            silence_used_ms = float(
                getattr(segment, "silence_ms", 0.0)
                or getattr(self._segmenter, "current_silence_ms", 0.0)
                or 0.0
            )
            heuristic_class = classify_utterance(
                self._partial_heuristic_text or self._partial_text,
            ).value
            last_speech_sample_index = getattr(segment, "last_speech_sample_index", None)
            if last_speech_sample_index is None:
                last_speech_sample_index = len(segment.audio) // 2
            last_speech_absolute_sample_index = (
                segment_start_sample + int(last_speech_sample_index)
            )
            wakeword_position_samples = (
                max(0, wakeword_hit_sample - segment_start_sample)
                if wakeword_hit_sample is not None else None
            )
            wakeword_position_ms = (
                wakeword_position_samples * 1000 / SAMPLE_RATE
                if wakeword_position_samples is not None else None
            )

            if not wake_fired and not self._conversation_active_fn():
                await self._record_segment(
                    segment.audio,
                    transcript=None,
                    partial_transcripts=list(self._partial_transcripts),
                    wakeword_position_samples=wakeword_position_samples,
                    wakeword_position_ms=wakeword_position_ms,
                    silence_used_ms=silence_used_ms,
                    turn_model_p=turn_model_p,
                    turn_model_ms=turn_model_ms,
                    turn_decision=turn_decision,
                    heuristic_class=heuristic_class,
                    last_speech_sample_index=last_speech_sample_index,
                )
                self._reset_segment_state()
                continue

            partial_runs = self._partial_runs
            partial_skip_reasons = dict(self._partial_skip_reasons)
            turn_started_at = time.perf_counter()
            stt_started = turn_started_at
            partial_text = self._partial_text
            task = self._partial_task
            partial_covers_speech = (
                task is not None
                and self._partial_run_generation == self._segment_generation
                and self._partial_audio_end_sample_index is not None
                and self._partial_audio_end_sample_index >= last_speech_absolute_sample_index
                and (
                    not wake_fired
                    or self._partial_audio_start_sample_index is not None
                    and self._partial_audio_start_sample_index >= crop_start_sample
                )
            )
            text = None
            raw_text = ""
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
                transcript = await transcribe_pcm(stt_audio)
                if isinstance(transcript, tuple) and len(transcript) == 2:
                    raw_text, wakeword_hint = transcript
                else:
                    raw_text = transcript
                    from core.voice import has_voice_wakeword_hint

                    wakeword_hint = has_voice_wakeword_hint(str(raw_text or ""))
                raw_text = str(raw_text or "").strip()
                text = normalize_wakeword_transcript(
                    raw_text, wakeword_triggered=wake_fired,
                )
            elif partial_as_final:
                raw_text = str(self._partial_raw_text or text or "").strip()
                from core.voice import has_voice_wakeword_hint

                wakeword_hint = bool(
                    self._partial_wakeword_hint
                    or has_voice_wakeword_hint(raw_text)
                )
                text = normalize_wakeword_transcript(
                    raw_text, wakeword_triggered=wake_fired,
                )
            partial_text = self._partial_text or partial_text
            stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
            partial_transcripts = list(self._partial_transcripts)
            result = {
                "text": text,
                "raw_text": raw_text,
                "pcm_audio": bytes(segment.audio),
                "speech_pcm_audio": bytes(getattr(segment, "speech_audio", b"")),
                "addressed": True,
                "wakeword_triggered": wake_fired,
                "wakeword_hint": bool(wakeword_hint),
                "stt_ms": stt_ms,
                "turn_started_at": turn_started_at,
                "partial_runs": partial_runs,
                "partial_skip_reasons": partial_skip_reasons,
                "partial_text": partial_text,
                "partial_as_final": partial_as_final,
                "turn_model_p": turn_model_p,
                "turn_model_ms": turn_model_ms,
                "turn_decision": turn_decision,
                "silence_used_ms": silence_used_ms,
                "heuristic_class": heuristic_class,
                "wakeword_position_ms": wakeword_position_ms,
                "wakeword_position_samples": wakeword_position_samples,
                "partial_transcripts": partial_transcripts,
                "bargein": self._bargein_notified,
                "overlapping": self._segment_overlapped,
                "segment_generation": self._segment_generation,
            }
            await self._record_segment(
                segment.audio,
                transcript=text,
                partial_transcripts=partial_transcripts,
                wakeword_position_samples=wakeword_position_samples,
                wakeword_position_ms=wakeword_position_ms,
                silence_used_ms=silence_used_ms,
                turn_model_p=turn_model_p,
                turn_model_ms=turn_model_ms,
                turn_decision=turn_decision,
                heuristic_class=heuristic_class,
                last_speech_sample_index=last_speech_sample_index,
            )
            self._reset_segment_state()

        return result

    async def _record_segment(self, audio: bytes, **metadata) -> None:
        if not getattr(config, "VOICE_RECORD_SEGMENTS", False):
            return
        try:
            await asyncio.to_thread(record_voice_segment, bytes(audio), metadata)
        except Exception as exc:
            log.debug("Voice-Segment konnte nicht gespeichert werden (%s)", type(exc).__name__)

    def _set_smart_turn_mode(self, mode: str | None) -> None:
        setter = getattr(self._segmenter, "set_smart_turn_mode", None)
        if callable(setter):
            setter(mode)

    def _cancel_smart_turn_task(self) -> None:
        task = self._smart_turn_task
        if task is not None and not task.done():
            task.cancel()
        self._smart_turn_task = None
        self._smart_turn_task_generation = None

    def _reset_smart_turn_for_resumed_speech(self) -> None:
        self._cancel_smart_turn_task()
        self._smart_turn_check_silence_ms = None
        self._next_smart_turn_check_ms = None
        self._turn_model_p = None
        self._turn_decision = (
            "fallback" if self._smart_turn_failed
            else "heuristic" if not self._smart_turn_enabled else None
        )
        self._set_smart_turn_mode("fallback" if self._smart_turn_failed else None)

    def _maybe_start_smart_turn_warmup(self) -> None:
        if (
            not self._smart_turn_enabled
            or self._smart_turn is None
            or self._smart_turn_failed
            or self._smart_turn_warmup_started
            or not callable(getattr(self._smart_turn, "warmup", None))
        ):
            return
        self._smart_turn_warmup_started = True
        self._smart_turn_warmup_task = asyncio.create_task(
            self._warm_smart_turn(), name="voice-smart-turn-warmup",
        )

    async def _warm_smart_turn(self) -> None:
        try:
            await self._smart_turn.warmup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed:
                return
            self._smart_turn_failed = True
            self._turn_decision = "fallback"
            self._set_smart_turn_mode("fallback")
            from core.smart_turn import warn_unavailable_once

            warn_unavailable_once(exc)

    def _collect_smart_turn_result(self) -> None:
        task = self._smart_turn_task
        if task is None or not task.done():
            return
        generation = self._smart_turn_task_generation
        self._smart_turn_task = None
        self._smart_turn_task_generation = None
        if task.cancelled() or generation != self._segment_generation:
            return
        try:
            prediction = task.result()
            if hasattr(prediction, "probability"):
                probability = float(prediction.probability)
                elapsed_ms = float(getattr(prediction, "elapsed_ms", 0.0))
            else:
                probability = float(prediction)
                elapsed_ms = 0.0
            if not 0 <= probability <= 1:
                raise ValueError("Smart-Turn-Ausgabe liegt außerhalb [0, 1]")
        except Exception as exc:
            self._smart_turn_failed = True
            self._turn_decision = "fallback"
            self._set_smart_turn_mode("fallback")
            from core.smart_turn import warn_unavailable_once

            warn_unavailable_once(exc)
            return

        self._turn_model_p = probability
        self._turn_model_ms += max(0.0, elapsed_ms)
        complete_at = float(getattr(config, "VOICE_SMART_TURN_COMPLETE", 0.6))
        incomplete_below = float(getattr(config, "VOICE_SMART_TURN_INCOMPLETE", 0.3))
        if probability >= complete_at:
            self._turn_decision = "model_complete"
            self._next_smart_turn_check_ms = None
            self._set_smart_turn_mode("model_complete")
        elif probability < incomplete_below:
            self._turn_decision = "model_incomplete"
            self._next_smart_turn_check_ms = (
                float(self._smart_turn_check_silence_ms or 0.0) + 300.0
            )
            self._set_smart_turn_mode("model_incomplete")
        else:
            self._turn_decision = "heuristic"
            self._next_smart_turn_check_ms = None
            self._set_smart_turn_mode("heuristic")

    def _maybe_start_smart_turn(self) -> None:
        if (
            not self._smart_turn_enabled
            or self._smart_turn is None
            or self._smart_turn_failed
            or self._closed
            or getattr(self._segmenter, "is_speaking", None) is not True
            or getattr(self._segmenter, "current_chunk_is_speech", None) is True
        ):
            return
        try:
            silence_ms = float(getattr(self._segmenter, "current_silence_ms", 0.0))
        except (TypeError, ValueError):
            return
        trigger_ms = float(getattr(config, "VOICE_SMART_TURN_TRIGGER_MS", 250))
        if silence_ms < trigger_ms:
            return
        if self._smart_turn_task is not None and not self._smart_turn_task.done():
            return
        if self._turn_decision in {"model_complete", "fallback"}:
            return
        if self._turn_decision == "heuristic" and self._turn_model_p is not None:
            return
        if (
            self._turn_decision == "model_incomplete"
            and self._next_smart_turn_check_ms is not None
            and silence_ms < self._next_smart_turn_check_ms
        ):
            return
        if getattr(self._smart_turn, "ready", None) is False:
            self._turn_decision = "heuristic"
            self._next_smart_turn_check_ms = None
            self._set_smart_turn_mode("heuristic")
            return
        audio = getattr(self._segmenter, "current_audio", b"")
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            return
        self._smart_turn_check_silence_ms = silence_ms
        self._set_smart_turn_mode("model_pending")
        self._smart_turn_task_generation = self._segment_generation
        self._smart_turn_task = asyncio.create_task(
            self._smart_turn.predict(bytes(audio)),
            name="voice-smart-turn",
        )

    def _discard_wakeword_hits_through(self, sample_index: int) -> None:
        while self._wakeword_hits and self._wakeword_hits[0] <= sample_index:
            self._wakeword_hits.popleft()
        # _wakeword_detected bleibt: Ein anhaltendes Detektorsignal ist keine neue Flanke.

    def _wakeword_fired_for_segment(self, start_sample: int, end_sample: int) -> bool:
        return self._wakeword_hit_for_range(
            start_sample, end_sample, consume=True,
        ) is not None

    def _wakeword_hit_for_range(
        self, start_sample: int, end_sample: int, *, consume: bool,
    ) -> int | None:
        preroll_samples = int(
            max(0, float(getattr(config, "VOICE_WAKE_PREROLL_MS", 1500)))
            * SAMPLE_RATE / 1000
        )
        earliest_sample = max(0, start_sample - preroll_samples)
        while self._wakeword_hits and self._wakeword_hits[0] < earliest_sample:
            self._wakeword_hits.popleft()
        hit = next((
            value for value in self._wakeword_hits
            if earliest_sample <= value <= end_sample
        ), None)
        if consume:
            self._discard_wakeword_hits_through(end_sample)
        return hit

    def _wakeword_fired_for_partial_audio(
        self, audio: bytes, audio_end_sample_index: int,
    ) -> tuple[bytes, int, bool]:
        segment_start = max(0, audio_end_sample_index - len(audio) // 2)
        hit = self._wakeword_hit_for_range(
            segment_start, audio_end_sample_index, consume=False,
        )
        cropped, crop_start = crop_wakeword_audio(
            audio,
            segment_start_sample=segment_start,
            wakeword_hit_sample=hit,
        )
        return cropped, crop_start, hit is not None

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
        audio_end_sample_index = self._vad_samples_processed
        audio, audio_start_sample_index, wakeword_fired = self._wakeword_fired_for_partial_audio(
            bytes(current_audio), audio_end_sample_index,
        )
        audio_seconds = len(audio) / (2 * SAMPLE_RATE)
        predicted_ms = stt_runtime.estimate_stt_runtime_ms(audio_seconds)
        if predicted_ms > float(getattr(config, "VOICE_PARTIAL_MAX_MS", 600)):
            self._partial_skip_reasons["budget"] += 1
            return
        generation = self._segment_generation
        self._partial_audio_end_sample_index = self._vad_samples_processed
        self._partial_audio_start_sample_index = audio_start_sample_index
        self._partial_wakeword_fired = wakeword_fired
        self._partial_run_generation = generation
        self._partial_task = asyncio.create_task(
            self._run_partial(
                audio, generation, len(current_audio) // 2,
                wakeword_triggered=wakeword_fired,
                segment_offset_ms=(len(current_audio) // 2) * 1000 / SAMPLE_RATE,
            ),
            name="voice-partial-stt",
        )
        self._partial_runs += 1

    async def _run_partial(
        self,
        audio: bytes,
        generation: int,
        audio_end_sample_index: int,
        *,
        wakeword_triggered: bool = False,
        segment_offset_ms: float | None = None,
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
        raw_text = str(text).strip()
        if not raw_text:
            return None
        text = normalize_wakeword_transcript(
            raw_text, wakeword_triggered=wakeword_triggered,
        )
        if generation == self._segment_generation and not self._closed and (
            getattr(self._segmenter, "is_speaking", None) is True or self._finalizing
        ):
            self._partial_text = text
            self._partial_raw_text = raw_text
            self._partial_heuristic_text = raw_text
            self._partial_wakeword_hint = bool(wakeword_hint)
            self._partial_transcripts.append({
                "offset_ms": round(
                    audio_end_sample_index * 1000 / SAMPLE_RATE
                    if segment_offset_ms is None else segment_offset_ms,
                    1,
                ),
                "text": text,
            })
            setter = getattr(self._segmenter, "set_partial_transcript", None)
            if callable(setter):
                try:
                    setter(raw_text, audio_end_sample_index=audio_end_sample_index)
                except TypeError:
                    setter(raw_text)
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
        self._cancel_smart_turn_task()
        warmup_task = self._smart_turn_warmup_task
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
        if warmup_task is not None:
            await asyncio.gather(warmup_task, return_exceptions=True)
        self._smart_turn_warmup_task = None
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
