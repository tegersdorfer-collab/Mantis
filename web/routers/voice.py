"""
Voice — API-Router für die Desktop-Sprachsteuerung (Phase 5a Erfassung + Phase 5b
Agent-Anbindung + TTS-Antwort).
Nimmt vom Tauri-Client hochgeladene Audio-Segmente entgegen, transkribiert sie
lokal, prüft ob sie an Mantis gerichtet sind, und lässt bei Adressierung den
echten Agenten (orch.dashboard_respond) antworten — Antwort kommt als Text UND
als synthetisierte Sprache (Piper-TTS, base64) zurück, damit der Desktop-Client
sie direkt abspielen kann.
"""
import base64
import asyncio
import inspect
import json
import logging
import math
import re
import tempfile
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, WebSocket, WebSocketDisconnect

import config
from core import db, voice_fillers, voice_timing
from core.voice import VoiceConversationWindow, transcribe_audio, is_addressed_to_mantis
from core.voice_prework import VoicePreworkCache
from core.voice_stream import VoiceStreamSession
from core.stream_events import StreamEvent
from core.tts import _first_early_stream_boundary, synthesize, synthesize_stream

log = logging.getLogger("mantis.api")


@dataclass(slots=True)
class _PlaybackBlock:
    block_id: str
    sentence_id: int | None
    text: str | None
    duration_ms: float | None
    played_ms: float = 0.0
    finished: bool = False
    skipped: bool = False
    sent_at: float | None = None
    playback_end_at: float | None = None


@dataclass(slots=True)
class _PlaybackSentence:
    text: str
    block_ids: list[str] = field(default_factory=list)
    complete: bool = False


class _PlaybackTurn:
    """Fortschritt eines einzelnen Antwort-Audiostreams, nach Block-ID getrennt."""

    def __init__(self):
        self.turn_id = uuid4().hex
        self.blocks: dict[str, _PlaybackBlock] = {}
        self.sentences: dict[int, _PlaybackSentence] = {}
        self.audio_end_count: int | None = None
        self.interrupted = False
        self.bargein_ms: float | None = None
        self.progress_reported = False
        self.timed_out = False
        self._first_block_sent_at: float | None = None
        self._playback_started_at: float | None = None
        self.changed = asyncio.Event()
        self.stop_ack = asyncio.Event()

    def register_block(
        self, seq: int, audio: bytes, *, text: str | None = None,
        sentence_id: int | None = None,
    ) -> str:
        block_id = f"{self.turn_id}:{seq}"
        duration_ms = voice_fillers._ogg_opus_duration_ms(audio)
        self.blocks[block_id] = _PlaybackBlock(
            block_id, sentence_id, text, duration_ms,
        )
        if sentence_id is not None and text is not None:
            sentence = self.sentences.setdefault(
                sentence_id, _PlaybackSentence(text),
            )
            sentence.block_ids.append(block_id)
        self.changed.set()
        return block_id

    def mark_sentence_complete(self, sentence_id: int) -> None:
        sentence = self.sentences.get(sentence_id)
        if sentence is not None:
            sentence.complete = True
            self.changed.set()

    def mark_block_sent(self, block_id: str, *, sent_at: float | None = None) -> bool:
        block = self.blocks.get(str(block_id))
        if block is None:
            return False
        timestamp = time.monotonic() if sent_at is None else float(sent_at)
        block.sent_at = timestamp
        if self._first_block_sent_at is None:
            self._first_block_sent_at = timestamp
        if self._playback_started_at is None:
            self._playback_started_at = timestamp
        self.changed.set()
        return True

    @staticmethod
    def _duration_for(block: _PlaybackBlock) -> float:
        if block.duration_ms is not None and block.duration_ms > 0:
            return block.duration_ms
        return max(350.0, len((block.text or "").split()) * 350.0)

    def _completion_timeout_s(self) -> float:
        total_ms = sum(
            self._duration_for(block)
            for block in self.blocks.values()
            if block.sent_at is not None
        )
        return total_ms / 1000.0 + 2.0

    @property
    def assistant_text(self) -> str:
        return " ".join(
            sentence.text
            for sentence in self.sentences.values()
            if any(self.blocks[block_id].sent_at is not None for block_id in sentence.block_ids)
        ).strip()

    def estimate_progress(self, *, now: float | None = None) -> None:
        """Schätzt ohne Clientmeldungen, wie viele gesendete Blöcke bereits liefen."""
        playback_started_at = self._playback_started_at or self._first_block_sent_at
        if playback_started_at is None:
            return
        current = time.monotonic() if now is None else float(now)
        elapsed_ms = max(0.0, (current - playback_started_at) * 1000.0)
        offset_ms = 0.0
        for block in self.blocks.values():
            if block.sent_at is None:
                continue
            duration_ms = self._duration_for(block)
            played_ms = min(duration_ms, max(0.0, elapsed_ms - offset_ms))
            block.played_ms = max(block.played_ms, played_ms)
            if played_ms >= max(0.0, duration_ms - 35.0):
                block.finished = True
                block.played_ms = duration_ms
                if block.playback_end_at is None:
                    block.playback_end_at = playback_started_at + (
                        offset_ms + duration_ms
                    ) / 1000.0
            offset_ms += duration_ms
        self.changed.set()

    def record_progress(
        self, block_id: str, played_ms: float, *, finished: bool = False,
        skipped: bool = False, now: float | None = None,
    ) -> bool:
        block = self.blocks.get(str(block_id))
        if block is None:
            return False
        try:
            played = max(0.0, float(played_ms))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(played):
            return False
        current = time.monotonic() if now is None else float(now)
        self.progress_reported = True
        if block.duration_ms is not None:
            played = min(played, block.duration_ms)
        offset_ms = 0.0
        for previous in self.blocks.values():
            if previous is block:
                break
            if previous.sent_at is not None:
                offset_ms += self._duration_for(previous)
        if block.sent_at is not None:
            self._playback_started_at = current - (offset_ms + played) / 1000.0
        block.played_ms = max(block.played_ms, played)
        block.skipped = block.skipped or bool(skipped)
        block.finished = block.finished or bool(finished) or block.skipped or (
            block.duration_ms is not None
            and block.played_ms >= max(0.0, block.duration_ms - 35.0)
        )
        if block.finished and not block.skipped and block.duration_ms is not None:
            block.played_ms = block.duration_ms
        if block.finished:
            block.playback_end_at = current
        self.changed.set()
        return True

    def echo_window_text(
        self, *, now: float | None = None, window_s: float = 2.0,
    ) -> str:
        """Text gesendeter Blöcke, die noch laufen oder höchstens zwei Sekunden liefen."""
        playback_started_at = self._playback_started_at or self._first_block_sent_at
        if playback_started_at is None:
            return ""
        current = time.monotonic() if now is None else float(now)
        cursor = playback_started_at
        included_sentences: set[int] = set()
        texts: list[str] = []
        for block in self.blocks.values():
            if block.sent_at is None:
                continue
            block_start = max(cursor, block.sent_at)
            estimated_end = block.playback_end_at or (
                block_start + self._duration_for(block) / 1000.0
            )
            cursor = estimated_end
            if block.skipped:
                continue
            pending = not block.finished and estimated_end > current
            recently_played = 0.0 <= current - estimated_end < window_s
            if not (pending or recently_played) or not block.text:
                continue
            if block.sentence_id is not None:
                if block.sentence_id in included_sentences:
                    continue
                included_sentences.add(block.sentence_id)
                sentence = self.sentences.get(block.sentence_id)
                texts.append(sentence.text if sentence is not None else block.text)
            elif block.text not in texts:
                texts.append(block.text)
        return " ".join(texts).strip()

    def progress_ms_for(self, block_id: str) -> float | None:
        block = self.blocks.get(str(block_id))
        return block.played_ms if block is not None else None

    @property
    def playback_active(self) -> bool:
        return any(not block.finished for block in self.blocks.values())

    def mark_audio_end(self, count: int) -> None:
        self.audio_end_count = max(0, int(count))
        self.changed.set()

    @property
    def complete(self) -> bool:
        if self.audio_end_count is None or len(self.blocks) < self.audio_end_count:
            return False
        return all(block.finished for block in self.blocks.values())

    async def wait_until_complete(self) -> None:
        try:
            async with asyncio.timeout(self._completion_timeout_s()):
                while not self.complete:
                    self.changed.clear()
                    if self.complete:
                        return
                    await self.changed.wait()
        except TimeoutError:
            self.estimate_progress()
            if self.complete:
                return
            self.timed_out = True
            raise

    def spoken_text(self) -> str:
        parts: list[str] = []
        for sentence in self.sentences.values():
            blocks = [self.blocks[block_id] for block_id in sentence.block_ids]
            if not blocks:
                break
            played_ms = sum(block.played_ms for block in blocks)
            if played_ms <= 0:
                break
            if (
                sentence.complete and all(block.finished for block in blocks)
                and not any(block.skipped for block in blocks)
            ):
                parts.append(sentence.text)
                continue

            words = sentence.text.split()
            known_duration = sum(
                block.duration_ms or 0.0 for block in blocks
            )
            estimated_duration = max(known_duration, len(words) * 350.0, 1.0)
            fraction = min(1.0, played_ms / estimated_duration)
            spoken_words = max(0, min(len(words), int(len(words) * fraction)))
            if spoken_words:
                parts.append(" ".join(words[:spoken_words]))
            break
        return " ".join(parts).strip()


def _voice_backend_details(orch) -> tuple[str, str]:
    agent = getattr(orch, "voice_agent", None)
    backend = getattr(agent, "backend", None) or getattr(agent, "_backend", None)
    used_backend = getattr(backend, "last_used_backend", None)
    selected = used_backend or backend
    model = getattr(selected, "model_name", "unknown")
    stage = getattr(selected, "provider_name", type(selected).__name__ if selected else "unknown")
    return str(model), str(stage)


def _record_voice_latency(
    orch,
    started_at: float,
    *,
    stt_ms: float | None,
    address_check_ms: float | None = None,
    first_token_ms: float | None = None,
    first_tts_block_ms: float | None = None,
    first_answer_audio_ms: float | None = None,
    filler: str | None = None,
    filler_ms: float | None = None,
    bargein: bool = False,
    bargein_ms: float | None = None,
) -> dict:
    model, stage = _voice_backend_details(orch)
    detail = {
        "stt_ms": stt_ms,
        "address_check_ms": address_check_ms,
        "first_token_ms": first_token_ms,
        "first_tts_block_ms": first_tts_block_ms,
        "first_answer_audio_ms": first_answer_audio_ms,
        "filler": filler,
        "filler_ms": filler_ms,
        "bargein": bool(bargein),
        "bargein_ms": bargein_ms,
        "total_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "model": model,
        "stage": stage,
        **voice_timing.snapshot(),
    }
    log.info("voice_latency %s", detail)
    db.log_event("voice_latency", "Voice-Turn", detail)
    return detail


def _mark_reflex(trace) -> None:
    if any(isinstance(item, dict) and item.get("reflex") is True for item in (trace or [])):
        voice_timing.mark_reflex()


async def sende_antwort(
    websocket,
    text: str,
    reply: str | None,
    audio_streaming: bool,
    *,
    started_at: float | None = None,
) -> float | None:
    """Schickt die Antwort — als Tonblöcke (audio_streaming) oder als ein audio_b64.

    Blockweise beginnt die Wiedergabe nach dem ersten Satz statt nach der ganzen
    Antwort (bei vier Sätzen ~0,6 s statt ~4,5 s). Der Client hält das Mikrofon
    stummgeschaltet, bis audio_end kommt — deshalb MUSS audio_end auch im
    Fehlerfall gesendet werden, sonst bleibt er dauerhaft stumm.
    """
    if started_at is None:
        started_at = time.perf_counter()
    first_answer_audio_ms = None
    if not (audio_streaming and reply):
        audio_b64 = None
        if reply is not None:
            try:
                ogg = await synthesize(reply)
            except Exception as e:
                log.error(f"TTS für Voice-Antwort fehlgeschlagen: {e}")
                ogg = b""
            if ogg:
                first_answer_audio_ms = round((time.perf_counter() - started_at) * 1000, 1)
                audio_b64 = base64.b64encode(ogg).decode("ascii")
        await websocket.send_json(
            {"text": text, "addressed": True, "reply": reply, "audio_b64": audio_b64})
        return first_answer_audio_ms

    await websocket.send_json({
        "text": text, "addressed": True, "reply": reply,
        "audio_b64": None, "audio_streaming": True,
    })
    # Erzeugen und Senden werden getrennt behandelt: Starlette meldet einen toten
    # Client je nach ASGI-Server als RuntimeError — genau das kann aber auch aus der
    # Synthese kommen. Ein TTS-Fehler beendet nur den Ton (audio_end folgt trotzdem),
    # ein Sendefehler fliegt nach oben und beendet die Verbindung.
    seq = 0
    bloecke = synthesize_stream(reply)
    while True:
        try:
            ogg = await anext(bloecke)
        except StopAsyncIteration:
            break
        except Exception as e:
            log.error(f"TTS-Streaming fehlgeschlagen: {e}")
            break
        if ogg and first_answer_audio_ms is None:
            first_answer_audio_ms = round((time.perf_counter() - started_at) * 1000, 1)
        await websocket.send_json({
            "type": "audio_chunk", "seq": seq,
            "b64": base64.b64encode(ogg).decode("ascii"),
        })
        seq += 1
    await websocket.send_json({"type": "audio_end", "count": seq})
    return first_answer_audio_ms


def _sentence_complete(text: str) -> bool:
    return bool(re.search(r"[.!?…][\"'»”’)}\]]*$", text.strip()))


def _normalized_speech(text: str) -> str:
    return " ".join(text.split())


_VOICE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_VOICE_ECHO_FILLER_WORDS = frozenset({
    "äh", "ähm", "hm", "hmm", "also", "bitte", "mal", "kurz", "eben",
    "schnell", "gern", "gerne", "vielleicht",
})


def _is_recent_tts_echo(
    text: str, echo_texts,
) -> bool:
    """Erkennt nur wortgleiche, zusammenhängende TTS-Echos (nach Füllwort-Entfernung)."""
    def tokens(value: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", str(value)).casefold()
        return [
            token for token in _VOICE_TOKEN_RE.findall(normalized)
            if token not in _VOICE_ECHO_FILLER_WORDS
        ]

    transcript_tokens = tokens(text)
    if not transcript_tokens:
        return False
    if isinstance(echo_texts, str):
        echo_texts = [echo_texts]
    size = len(transcript_tokens)
    for spoken_text in echo_texts:
        spoken_tokens = tokens(spoken_text)
        for start in range(len(spoken_tokens) - size + 1):
            if spoken_tokens[start:start + size] == transcript_tokens:
                return True
    return False


async def sende_streaming_antwort(
    websocket,
    text: str,
    orch,
    *,
    stt_ms: float | None = None,
    address_check_ms: float | None = None,
    started_at: float | None = None,
    prework=None,
    partial_runs: int = 0,
    partial_as_final: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    playback: _PlaybackTurn | None = None,
    defer_history: bool = False,
    wait_for_playback: bool = False,
    interrupt_check: Callable[[], bool] | None = None,
    on_tts_text_sent: Callable[[str, float], None] | None = None,
) -> str | None:
    """Leitet fertige Antwortsätze parallel zur laufenden Cloud-Antwort an TTS."""
    if started_at is None:
        started_at = time.perf_counter()
    playback = playback or _PlaybackTurn()
    first_token_ms = None
    first_tts_block_ms = None
    first_answer_audio_ms = None
    filler_kind: str | None = None
    filler_ms: float | None = None
    pending_filler = None
    response_audio_ready = False
    ack_done = not getattr(config, "VOICE_FILLERS_ENABLED", True)
    sequence = 0
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    audio_queue: asyncio.Queue[
        tuple[str, int | None, str | None, bytes | None] | None
    ] = asyncio.Queue()
    state_changed = asyncio.Event()
    attempts: dict[str, dict[str, str | bool]] = {}
    has_queued_sentence = False
    spoken_sentences: list[str] = []
    response: list[tuple[str | None, list, Exception | None]] = []
    response_trace: list = []
    next_sentence_id = 0
    ack_task: asyncio.Task | None = None

    def request_filler(kind: str) -> None:
        nonlocal filler_kind, pending_filler
        timing = voice_timing.current()
        if (
            not getattr(config, "VOICE_FILLERS_ENABLED", True)
            or filler_kind is not None
            or response_audio_ready
            or (timing is not None and timing.reflex)
        ):
            return
        selected = voice_fillers.choose_cached_filler(kind)
        if selected is None:
            return
        filler_kind = selected.kind
        pending_filler = selected
        state_changed.set()

    async def send_audio_chunk(
        audio: bytes, *, answer: bool = False, sentence: str | None = None,
        sentence_id: int | None = None,
    ) -> None:
        nonlocal sequence, first_tts_block_ms, first_answer_audio_ms
        if first_tts_block_ms is None:
            first_tts_block_ms = round((time.perf_counter() - started_at) * 1000, 1)
        if answer and first_answer_audio_ms is None:
            first_answer_audio_ms = round((time.perf_counter() - started_at) * 1000, 1)
        block_id = playback.register_block(
            sequence, audio,
            text=sentence if answer else None,
            sentence_id=sentence_id if answer else None,
        )
        await websocket.send_json({
            "type": "audio_chunk",
            "seq": sequence,
            "block_id": block_id,
            "b64": base64.b64encode(audio).decode("ascii"),
        })
        sent_at = time.monotonic()
        playback.mark_block_sent(block_id, sent_at=sent_at)
        if answer and sentence and on_tts_text_sent is not None:
            on_tts_text_sent(sentence, sent_at)
        sequence += 1

    async def send_ack_after_delay() -> None:
        nonlocal ack_done
        try:
            delay_ms = max(0, int(getattr(config, "VOICE_FILLER_ACK_DELAY_MS", 800)))
            deadline = started_at + delay_ms / 1000
            await sleep(max(0, deadline - time.perf_counter()))
            # Gib einem bereits fertigen TTS-Block bei gleichzeitigem Wake-up den Vorrang.
            await asyncio.sleep(0)
            if not response_audio_ready:
                request_filler("ack")
        except asyncio.CancelledError:
            pass
        finally:
            ack_done = True
            state_changed.set()

    async def queue_sentence(sentence: str) -> None:
        nonlocal has_queued_sentence
        sentence = sentence.strip()
        if sentence:
            await queue.put(sentence)
            has_queued_sentence = True

    async def queue_final_text(final_text: str) -> None:
        for sentence in re.split(r"(?<=[.!?…])\s+", final_text):
            await queue_sentence(sentence)

    async def on_update(event: StreamEvent) -> None:
        nonlocal first_token_ms
        if not isinstance(event, StreamEvent):
            return
        if event.kind == "tool_start":
            if interrupt_check is not None and interrupt_check():
                raise asyncio.CancelledError
            request_filler("tool")
            return
        if event.kind == "attempt_start":
            attempts[event.attempt_id] = {
                "pending": "",
                "deferred": has_queued_sentence,
            }
            return
        if event.kind == "attempt_abort":
            attempts.pop(event.attempt_id, None)
            return
        if event.kind == "delta":
            if first_token_ms is None and event.text_delta:
                first_token_ms = round((time.perf_counter() - started_at) * 1000, 1)
            state = attempts.get(event.attempt_id)
            if state is None:
                return
            pending = str(state["pending"]) + event.text_delta
            if not state["deferred"] and not has_queued_sentence:
                first_sentence = re.split(r"(?<=[.!?…])\s+", pending, maxsplit=1)[0]
                boundary = _first_early_stream_boundary(first_sentence)
                if boundary is not None:
                    early_part = pending[:boundary].strip()
                    if early_part:
                        pending = pending[boundary:].lstrip()
                        await queue_sentence(early_part)
            sentences = re.split(r"(?<=[.!?…])\s+", pending)
            if len(sentences) > 1:
                if not state["deferred"]:
                    for sentence in sentences[:-1]:
                        await queue_sentence(sentence)
                state["pending"] = sentences[-1]
            else:
                state["pending"] = pending
            return
        if event.kind == "attempt_end":
            state = attempts.pop(event.attempt_id, None)
            if state is None:
                return
            pending = str(state["pending"])
            if _sentence_complete(pending) and not state["deferred"]:
                await queue_sentence(pending)

    async def produce_response() -> None:
        try:
            respond_kwargs = {"stream_cb": on_update}
            if prework is not None:
                respond_kwargs["prework"] = prework
            if defer_history:
                respond_kwargs["defer_finish"] = True
            if interrupt_check is not None:
                try:
                    parameters = inspect.signature(orch.voice_respond).parameters.values()
                except (TypeError, ValueError):
                    parameters = ()
                if any(
                    parameter.name == "interrupt_check"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                ):
                    respond_kwargs["interrupt_check"] = interrupt_check
            reply, trace = await orch.voice_respond(text, **respond_kwargs)
            _mark_reflex(trace)
            reply = reply or ""
            response_trace[:] = trace or []
            response.append((reply or None, response_trace, None))
            await queue.join()
            spoken_text = _normalized_speech(" ".join(spoken_sentences))
            final_text = _normalized_speech(reply)
            if not final_text:
                remainder = ""
            elif final_text.startswith(spoken_text):
                remainder = final_text[len(spoken_text):].strip()
            else:
                remainder = f"Korrektur: {reply}".strip()
            if remainder:
                await queue_final_text(remainder)
            await queue.join()
        except Exception as exc:
            response.append((None, [], exc))
            log.error("Voice-Cloud-Antwort fehlgeschlagen: %s", exc)
        finally:
            await queue.put(None)

    async def synthesize_sentences() -> None:
        nonlocal response_audio_ready, next_sentence_id
        try:
            while True:
                sentence = await queue.get()
                stream = None
                try:
                    if sentence is None:
                        return
                    sentence_id = next_sentence_id
                    next_sentence_id += 1
                    stream = synthesize_stream(sentence).__aiter__()
                    completed = False
                    has_audio = False
                    while True:
                        try:
                            ogg = await anext(stream)
                        except StopAsyncIteration:
                            completed = True
                            break
                        except Exception as exc:
                            log.error("TTS-Streaming fehlgeschlagen: %s", exc)
                            break
                        if ogg:
                            if not response_audio_ready:
                                response_audio_ready = True
                                if ack_task is not None and not ack_task.done():
                                    ack_task.cancel()
                            audio_queue.put_nowait(("audio", sentence_id, sentence, ogg))
                            state_changed.set()
                            has_audio = True
                    if completed and has_audio:
                        spoken_sentences.append(sentence)
                        audio_queue.put_nowait(("sentence_end", sentence_id, sentence, None))
                finally:
                    if stream is not None:
                        close = getattr(stream, "aclose", None)
                        if close is not None:
                            try:
                                await close()
                            except Exception as exc:
                                log.debug("TTS-Stream konnte nicht geschlossen werden: %s", exc)
                    queue.task_done()
        finally:
            audio_queue.put_nowait(None)
            state_changed.set()

    async def send_audio() -> None:
        nonlocal pending_filler, filler_ms
        response_finished = False
        has_pending_audio = False
        pending_audio: tuple[str, int | None, str | None, bytes | None] | None = None
        while True:
            if pending_filler is not None:
                selected = pending_filler
                pending_filler = None
                await send_audio_chunk(selected.audio)
                filler_ms = selected.duration_ms
                continue

            if not has_pending_audio:
                try:
                    pending_audio = audio_queue.get_nowait()
                    has_pending_audio = True
                except asyncio.QueueEmpty:
                    pass
            if has_pending_audio:
                item = pending_audio
                has_pending_audio = False
                pending_audio = None
                if item is None:
                    response_finished = True
                elif pending_filler is not None:
                    pending_audio = item
                    has_pending_audio = True
                elif item[0] == "sentence_end":
                    if item[1] is not None:
                        playback.mark_sentence_complete(item[1])
                else:
                    await send_audio_chunk(
                        item[3] or b"", answer=True,
                        sentence=item[2], sentence_id=item[1],
                    )
                continue

            if response_finished and ack_done:
                return

            state_changed.clear()
            audio_ready = asyncio.create_task(audio_queue.get())
            changed = asyncio.create_task(state_changed.wait())
            wait_tasks = (audio_ready, changed)
            try:
                done, _pending = await asyncio.wait(
                    wait_tasks, return_when=asyncio.FIRST_COMPLETED,
                )
                if audio_ready in done:
                    pending_audio = audio_ready.result()
                    has_pending_audio = True
                if changed in done:
                    await changed
            finally:
                for task in wait_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*wait_tasks, return_exceptions=True)

    timing_token = voice_timing.start()
    timing = voice_timing.current()
    timing.partial_runs = max(0, int(partial_runs))
    timing.partial_as_final = bool(partial_as_final)
    if getattr(config, "VOICE_FILLERS_ENABLED", True):
        ack_task = asyncio.create_task(send_ack_after_delay(), name="voice-filler-ack-delay")
    responder = asyncio.create_task(produce_response())
    synthesizer = asyncio.create_task(synthesize_sentences())
    sender = asyncio.create_task(send_audio())
    response_error = None
    reply = None
    trace = []
    client_disconnected = False
    cancelled_during_playback_wait = False

    async def record_interrupted_turn() -> None:
        try:
            await asyncio.wait_for(playback.stop_ack.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
        if not playback.progress_reported:
            playback.estimate_progress()
        recorder = getattr(orch, "record_interrupted_voice_turn", None)
        if callable(recorder):
            recorder(playback.spoken_text())

    try:
        tasks = (responder, synthesizer, sender, ack_task)
        await asyncio.gather(*(task for task in tasks if task is not None))
        reply, trace, response_error = response[0]
        await websocket.send_json({
            "text": text,
            "addressed": True,
            "reply": reply,
            "audio_b64": None,
        })
    except BaseException as exc:
        client_disconnected = isinstance(
            exc, (WebSocketDisconnect, RuntimeError, asyncio.CancelledError),
        )
        for task in (responder, synthesizer, sender, ack_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (responder, synthesizer, sender, ack_task) if task is not None),
            return_exceptions=True,
        )
        raise
    finally:
        for task in (responder, synthesizer, sender, ack_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (responder, synthesizer, sender, ack_task) if task is not None),
            return_exceptions=True,
        )
        try:
            if playback.interrupted:
                await record_interrupted_turn()
            elif not client_disconnected:
                await websocket.send_json({"type": "audio_end", "count": sequence})
                playback.mark_audio_end(sequence)
                if wait_for_playback or defer_history:
                    try:
                        await playback.wait_until_complete()
                    except TimeoutError:
                        log.warning("Voice-Playback meldete keinen Fortschritt rechtzeitig")
                    except asyncio.CancelledError:
                        cancelled_during_playback_wait = True
                        client_disconnected = not playback.interrupted
                    if playback.interrupted:
                        await record_interrupted_turn()
                    elif (
                        defer_history and not playback.timed_out
                        and response_error is None
                    ):
                        finish = getattr(orch, "finish_voice_turn", None)
                        if callable(finish) and reply:
                            finish(text, reply, trace)
        finally:
            try:
                _record_voice_latency(
                    orch, started_at, stt_ms=stt_ms,
                    address_check_ms=address_check_ms,
                    first_token_ms=first_token_ms,
                    first_tts_block_ms=first_tts_block_ms,
                    first_answer_audio_ms=first_answer_audio_ms,
                    filler=filler_kind,
                    filler_ms=filler_ms,
                    bargein=playback.interrupted,
                    bargein_ms=playback.bargein_ms,
                )
            finally:
                voice_timing.reset(timing_token)
    if cancelled_during_playback_wait:
        raise asyncio.CancelledError
    if response_error is not None:
        raise response_error
    return reply


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/voice/segment")
    async def voice_segment(audio: UploadFile = File(...)):
        started_at = time.perf_counter()
        stt_ms = None
        address_check_ms = None
        first_tts_block_ms = None
        first_answer_audio_ms = None
        reply = None
        audio_b64 = None
        timing_token = None
        try:
            suffix = Path(audio.filename or "segment.wav").suffix or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
                tmp.write(await audio.read())
                tmp.flush()
                stt_started = time.perf_counter()
                text = await transcribe_audio(tmp.name)
                stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)

            if text:
                address_started = time.perf_counter()
                addressed = await is_addressed_to_mantis(text)
                address_check_ms = round((time.perf_counter() - address_started) * 1000, 1)
            else:
                addressed = False

            if addressed and orch is not None:
                timing_token = voice_timing.start()
                reply, trace = await orch.voice_respond(text)
                _mark_reflex(trace)
                try:
                    ogg = await synthesize(reply)
                except Exception as e:
                    log.error(f"TTS für Voice-Antwort fehlgeschlagen: {e}")
                    ogg = b""
                if ogg:
                    first_answer_audio_ms = round((time.perf_counter() - started_at) * 1000, 1)
                    audio_b64 = base64.b64encode(ogg).decode("ascii")
                    first_tts_block_ms = round((time.perf_counter() - started_at) * 1000, 1)
        finally:
            try:
                _record_voice_latency(
                    orch, started_at, stt_ms=stt_ms,
                    address_check_ms=address_check_ms,
                    first_tts_block_ms=first_tts_block_ms,
                    first_answer_audio_ms=first_answer_audio_ms,
                )
            finally:
                if timing_token is not None:
                    voice_timing.reset(timing_token)

        return {"text": text, "addressed": addressed, "reply": reply, "audio_b64": audio_b64}

    @router.get("/api/voice/stream-mode")
    async def voice_stream_mode():
        mode = db.get_setting("voice_stream_mode", "http") or "http"
        return {"mode": mode}

    @router.websocket("/ws/voice/stream")
    async def voice_stream(websocket: WebSocket):
        await websocket.accept()

        from core.vad import SileroVAD, VadSegmenter

        vad_model = SileroVAD(Path(__file__).parent.parent.parent / "data" / "vad" / "silero_vad.onnx")
        # VoiceStreamSession buffers raw incoming chunks into fixed 512-sample
        # (32ms @ 16kHz) frames before calling process_chunk(), so chunk_ms
        # here must match that buffered frame size (see core/voice_stream.py).
        segmenter = VadSegmenter(vad_model, chunk_ms=32)

        from core.wakeword import WakeWordDetector
        wakeword_path = Path(__file__).parent.parent.parent / "data" / "wakeword" / "mantis.onnx"
        wakeword_detector = WakeWordDetector(wakeword_path)
        prepare_voice_turn = getattr(orch, "prepare_voice_turn", None)
        prework_cache = (
            VoicePreworkCache(prepare_voice_turn)
            if callable(prepare_voice_turn) else None
        )
        muted = False
        # Streaming der Antwort-Audio ist OPT-IN: Der Client meldet sich nach dem
        # Connect mit {"type":"hello","audio":"stream"}. Ohne das bleibt es beim
        # bisherigen Verhalten (ein audio_b64 im Ergebnis-JSON) — die iOS-Apps und
        # der POST-Endpoint /api/voice/segment kennen das neue Protokoll nicht.
        audio_streaming = False
        supports_playback_progress = False
        active_playback: _PlaybackTurn | None = None
        active_response_task: asyncio.Task | None = None
        background_response_tasks: set[asyncio.Task] = set()
        playback_turns: dict[str, _PlaybackTurn] = {}
        recent_tts_turns: deque[_PlaybackTurn] = deque()
        conversation_window = VoiceConversationWindow()
        send_lock = asyncio.Lock()

        class SerializedWebSocket:
            async def send_json(self, data):
                async with send_lock:
                    await websocket.send_json(data)

        send_socket = SerializedWebSocket()

        def _response_task_done(task: asyncio.Task) -> None:
            background_response_tasks.discard(task)
            if not task.cancelled():
                try:
                    task.exception()
                except BaseException:
                    pass

        def _detach_active_response() -> None:
            nonlocal active_response_task
            task = active_response_task
            active_response_task = None
            if task is None or task.done():
                return
            task.cancel()
            background_response_tasks.add(task)
            task.add_done_callback(_response_task_done)

        def _remember_tts_text(text: str, _sent_at: float) -> None:
            conversation_window.mark_active(text)

        def _recent_tts_echo_texts(now: float) -> list[str]:
            echo_texts: list[str] = []
            retained_turns: deque[_PlaybackTurn] = deque()
            for playback in recent_tts_turns:
                echo_text = playback.echo_window_text(now=now)
                if echo_text:
                    echo_texts.append(echo_text)
                    retained_turns.append(playback)
                elif (
                    playback is active_playback
                    or not playback.complete and not playback.interrupted
                    and not playback.timed_out
                ):
                    retained_turns.append(playback)
            recent_tts_turns.clear()
            recent_tts_turns.extend(retained_turns)
            return echo_texts

        async def stop_for_bargein(detected_at: float) -> None:
            playback = active_playback
            if (
                playback is None or not playback.playback_active
                or playback.interrupted
            ):
                return
            playback.interrupted = True
            conversation_window.mark_active(
                playback.assistant_text or conversation_window.address_context(),
            )
            playback.bargein_ms = round(
                max(0.0, (time.monotonic() - detected_at) * 1000), 1,
            )
            _detach_active_response()
            await send_socket.send_json({
                "type": "stop_playback", "turn_id": playback.turn_id,
            })

        def conversation_active() -> bool:
            return active_playback is not None or conversation_window.is_active()

        session = VoiceStreamSession(
            vad_segmenter=segmenter,
            wakeword_detector=wakeword_detector,
            conversation_active_fn=conversation_active,
            on_partial=prework_cache.offer if prework_cache is not None else None,
            bargein_active_fn=lambda: (
                active_playback is not None and active_playback.playback_active
            ),
            on_bargein=stop_for_bargein,
        )

        async def run_streaming_response(
            result: dict, text: str, prework, playback: _PlaybackTurn,
        ) -> None:
            nonlocal active_response_task, active_playback
            try:
                reply = await sende_streaming_antwort(
                    send_socket, text, orch,
                    stt_ms=result.get("stt_ms"),
                    address_check_ms=result.get("address_check_ms"),
                    started_at=result.get("turn_started_at"),
                    prework=prework,
                    partial_runs=result.get("partial_runs", 0),
                    partial_as_final=result.get("partial_as_final", False),
                    playback=playback,
                    defer_history=callable(getattr(orch, "finish_voice_turn", None)),
                    wait_for_playback=(
                        supports_playback_progress
                        or callable(getattr(orch, "finish_voice_turn", None))
                    ),
                    interrupt_check=lambda: playback.interrupted,
                    on_tts_text_sent=_remember_tts_text,
                )
                conversation_window.mark_active(reply or playback.assistant_text)
            except asyncio.CancelledError:
                raise
            except (WebSocketDisconnect, RuntimeError) as e:
                log.info("Voice-WebSocket beim Antwortversand getrennt: %s", e)
            except Exception as e:
                log.error("Voice-Antworttask fehlgeschlagen: %s", e)
            finally:
                if active_playback is playback and (
                    playback.interrupted or playback.complete or playback.timed_out
                    or not supports_playback_progress
                ):
                    active_playback = None
                if (
                    playback.interrupted or playback.complete or playback.timed_out
                    or not supports_playback_progress
                ):
                    playback_turns.pop(playback.turn_id, None)
                if active_response_task is asyncio.current_task():
                    active_response_task = None

        async def process_voice_result(result: dict) -> None:
            nonlocal active_playback, active_response_task
            text = str(result.get("text") or "").strip()
            # Echo-Texte stammen vom Empfangszeitpunkt (Einreihen), nicht von der
            # Abarbeitung: langsame Adress-Checks davor dürfen das Fenster nicht schließen.
            echo_texts = list(result.pop("_echo_texts", ()))
            echo_texts.extend(_recent_tts_echo_texts(time.monotonic()))
            if _is_recent_tts_echo(text, echo_texts):
                log.debug("Voice-Transkript als Echo der letzten TTS-Blöcke verworfen")
                if prework_cache is not None:
                    prework_cache.clear()
                return

            address_started = time.perf_counter()
            conversation_context = conversation_window.address_context()

            async def check_address() -> bool:
                if conversation_context:
                    return await is_addressed_to_mantis(
                        text, conversation_context=conversation_context,
                    )
                return await is_addressed_to_mantis(text)

            address_task = asyncio.create_task(
                check_address(), name="voice-address-check",
            )
            addressed = await address_task
            result["address_check_ms"] = round(
                (time.perf_counter() - address_started) * 1000, 1,
            )
            if not addressed:
                if prework_cache is not None:
                    prework_cache.clear()
                return

            if active_playback is not None and active_playback.playback_active:
                await stop_for_bargein(time.monotonic())
            _detach_active_response()
            active_playback = None
            prework = prework_cache.take_for(text) if prework_cache is not None else None
            if prework_cache is not None:
                prework_cache.clear()
            if audio_streaming and orch is not None:
                playback = _PlaybackTurn()
                active_playback = playback
                playback_turns[playback.turn_id] = playback
                recent_tts_turns.append(playback)
                active_response_task = asyncio.create_task(
                    run_streaming_response(result, text, prework, playback),
                    name="voice-response-turn",
                )
                return

            started_at = result.get("turn_started_at", time.perf_counter())
            first_tts_block_ms = None
            first_answer_audio_ms = None
            reply = None
            timing_token = None
            try:
                if orch is not None:
                    timing_token = voice_timing.start()
                    timing = voice_timing.current()
                    timing.partial_runs = max(0, int(result.get("partial_runs", 0)))
                    timing.partial_as_final = bool(result.get("partial_as_final", False))
                    if prework is None:
                        reply, trace = await orch.voice_respond(text)
                    else:
                        reply, trace = await orch.voice_respond(text, prework=prework)
                    _mark_reflex(trace)
                    conversation_window.mark_active(reply)
                first_answer_audio_ms = await sende_antwort(
                    websocket, text, reply, audio_streaming, started_at=started_at,
                )
                first_tts_block_ms = (
                    round((time.perf_counter() - started_at) * 1000, 1)
                    if reply and not audio_streaming else None
                )
            except (WebSocketDisconnect, RuntimeError) as exc:
                log.info("Voice-WebSocket beim Antwortversand getrennt: %s", exc)
            finally:
                try:
                    _record_voice_latency(
                        orch, started_at, stt_ms=result.get("stt_ms"),
                        first_tts_block_ms=first_tts_block_ms,
                        first_answer_audio_ms=first_answer_audio_ms,
                    )
                finally:
                    if timing_token is not None:
                        voice_timing.reset(timing_token)

        turn_results: asyncio.Queue[dict] = asyncio.Queue()

        async def process_turn_results() -> None:
            while True:
                result = await turn_results.get()
                try:
                    await process_voice_result(result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("Voice-Turn konnte nicht verarbeitet werden: %s", exc)
                finally:
                    turn_results.task_done()

        turn_processor = asyncio.create_task(
            process_turn_results(), name="voice-turn-processor",
        )

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"] is not None:
                    result = await session.handle_chunk(message["bytes"], muted=muted)
                elif "text" in message and message["text"] is not None:
                    control = json.loads(message["text"])
                    if control.get("type") == "mute":
                        muted = bool(control.get("value", True))
                    elif control.get("type") == "hello":
                        audio_streaming = control.get("audio") == "stream"
                        supports_playback_progress = bool(
                            control.get("playback_progress", False),
                        )
                    elif control.get("type") == "playback_progress":
                        block_id = str(control.get("block_id", ""))
                        turn_id = block_id.rsplit(":", 1)[0] if ":" in block_id else ""
                        playback = playback_turns.get(turn_id)
                        if playback is not None:
                            playback.record_progress(
                                control.get("block_id", ""),
                                control.get("played_ms", 0),
                                finished=bool(control.get("finished", False)),
                                skipped=bool(control.get("skipped", False)),
                            )
                            if playback.complete and active_playback is playback:
                                active_playback = None
                    elif control.get("type") == "playback_stopped":
                        playback = playback_turns.get(str(control.get("turn_id", "")))
                        if (
                            playback is not None
                            and control.get("turn_id") == playback.turn_id
                        ):
                            playback.stop_ack.set()
                    continue
                else:
                    continue

                if result is None:
                    continue
                result["_echo_texts"] = _recent_tts_echo_texts(time.monotonic())
                turn_results.put_nowait(result)
        except WebSocketDisconnect:
            log.info("Voice-WebSocket-Verbindung geschlossen")
        except Exception as e:
            log.error(f"Voice-WebSocket-Handler abgebrochen: {e}")
        finally:
            try:
                if not turn_processor.done():
                    turn_processor.cancel()
                tasks_to_finish = set(background_response_tasks)
                if active_response_task is not None:
                    tasks_to_finish.add(active_response_task)
                tasks_to_finish.add(turn_processor)
                for task in tasks_to_finish:
                    if not task.done():
                        task.cancel()
                if tasks_to_finish:
                    await asyncio.gather(*tasks_to_finish, return_exceptions=True)
                await session.close()
            finally:
                if prework_cache is not None:
                    await prework_cache.close()

    return router
