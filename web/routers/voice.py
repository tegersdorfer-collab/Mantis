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
import wave
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    APIRouter, UploadFile, File, HTTPException, Request, WebSocket,
    WebSocketDisconnect,
)

import config
from core import db, tts, voice_fillers, voice_timing
from core.voice import (
    VoiceConversationWindow, has_voice_wakeword_hint,
    transcribe_audio_with_hint, is_addressed_to_mantis,
)
from core import fast_commands
from core.voice_prework import VoicePreworkCache
from core.voice_text import normalize_wakeword_transcript
from core.voice_stream import (
    VoiceStreamSession, partial_stt_executor_available, transcribe_pcm_partial,
)
from core.stream_events import StreamEvent
from core.tts import _first_early_stream_boundary, synthesize, synthesize_stream

log = logging.getLogger("mantis.api")

SPEAKER_ENROLL_MAX_CLIPS = 12
SPEAKER_ENROLL_MAX_BYTES = 4 * 1024 * 1024
SPEAKER_ENROLL_MAX_SECONDS = 90.0
SPEAKER_ENROLL_UPLOAD_TIMEOUT_SECONDS = 30.0
SPEAKER_ENROLL_SESSION_TTL_SECONDS = 10 * 60.0
SPEAKER_ENROLL_MAX_SESSIONS = 8


def _wav_duration_seconds(payload: bytes) -> float:
    """Liest die WAV-Dauer aus dem Header, ohne PCM-Frames zu dekodieren."""
    try:
        with wave.open(BytesIO(payload), "rb") as source:
            frame_rate = source.getframerate()
            if frame_rate <= 0:
                raise ValueError("Ungültige WAV-Datei")
            return source.getnframes() / frame_rate
    except (wave.Error, EOFError) as exc:
        raise ValueError("Ungültige WAV-Datei") from exc


async def _read_bounded_request_body(
    request: Request, *, max_bytes: int = SPEAKER_ENROLL_MAX_BYTES,
) -> bytes:
    """Liest einen Request-Body begrenzt direkt aus dem ASGI-Stream."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        declared_length = content_length.strip()
        if not declared_length.isascii() or not declared_length.isdigit():
            raise HTTPException(status_code=400, detail="Ungültiger Content-Length-Header")
        if int(declared_length) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail="Sprecher-Upload darf insgesamt höchstens 4 MB groß sein",
            )

    payload = bytearray()
    try:
        async with asyncio.timeout(SPEAKER_ENROLL_UPLOAD_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="Sprecher-Upload darf insgesamt höchstens 4 MB groß sein",
                    )
                payload.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=408,
            detail="Sprecher-Upload dauerte zu lange",
        ) from exc
    return bytes(payload)


@dataclass(slots=True)
class _SpeakerEnrollmentSession:
    clips: list[bytes] = field(default_factory=list)
    total_bytes: int = 0
    total_seconds: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    prefix_complete: bool = False


class _PlaybackTurn:
    """Fortschritt eines einzelnen Antwort-Audiostreams, nach Block-ID getrennt."""

    def __init__(self):
        self.turn_id = uuid4().hex
        self.blocks: dict[str, _PlaybackBlock] = {}
        self.sentences: dict[int, _PlaybackSentence] = {}
        self.audio_end_count: int | None = None
        self.interrupted = False
        self.bargein_ms: float | None = None
        self.bargein_decision: str | None = None
        self.bargein_started_at: float | None = None
        self.duck_started_at: float | None = None
        self.duck_ms = 0.0
        self.ducked = False
        self.progress_reported = False
        self.timed_out = False
        self._first_block_sent_at: float | None = None
        self._playback_started_at: float | None = None
        self.changed = asyncio.Event()
        self.stop_ack = asyncio.Event()

    def register_block(
        self, seq: int, audio: bytes, *, text: str | None = None,
        sentence_id: int | None = None, duration_ms: float | None = None,
    ) -> str:
        block_id = f"{self.turn_id}:{seq}"
        if duration_ms is None:
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

    def start_duck(self, *, now: float | None = None) -> bool:
        if self.ducked or self.interrupted or not self.playback_active:
            return False
        timestamp = time.monotonic() if now is None else float(now)
        self.ducked = True
        self.duck_started_at = timestamp
        if self.bargein_started_at is None:
            self.bargein_started_at = timestamp
        return True

    def finish_duck(
        self, *, now: float | None = None, decision: str | None = None,
    ) -> float:
        if self.ducked and self.duck_started_at is not None:
            timestamp = time.monotonic() if now is None else float(now)
            duration_ms = max(0.0, (timestamp - self.duck_started_at) * 1000)
            self.duck_ms = round(self.duck_ms + duration_ms, 1)
        else:
            duration_ms = 0.0
        self.ducked = False
        self.duck_started_at = None
        if decision is not None:
            self.bargein_decision = decision
        if decision == "ignored":
            self.bargein_started_at = None
        return duration_ms

    def current_duck_ms(self, *, now: float | None = None) -> float:
        duration_ms = self.duck_ms
        if self.ducked and self.duck_started_at is not None:
            timestamp = time.monotonic() if now is None else float(now)
            duration_ms += max(0.0, (timestamp - self.duck_started_at) * 1000)
        return round(duration_ms, 1)

    def mark_sentence_complete(self, sentence_id: int) -> None:
        sentence = self.sentences.get(sentence_id)
        if sentence is not None:
            sentence.complete = True
            self.changed.set()

    def mark_sentence_incomplete(self, sentence_id: int, synthesized_text: str) -> None:
        sentence = self.sentences.get(sentence_id)
        if sentence is not None:
            sentence.text = str(synthesized_text or "").strip()
            sentence.prefix_complete = True
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
        return self.spoken_text()

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
                (sentence.complete or sentence.prefix_complete)
                and all(block.finished for block in blocks)
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


class _AudioOutputGate:
    """Hält TTS und Audioversand bis zur Adressentscheidung zurück."""

    def __init__(self, *, open: bool = False):
        self._event = asyncio.Event()
        if open:
            self._event.set()
        self._waiting_at: float | None = None
        self._held_ms = 0.0
        self._resolved = bool(open)
        self._rejected = False

    @property
    def held_ms(self) -> float:
        return round(self._held_ms, 1)

    async def wait(self) -> None:
        if self._rejected:
            raise asyncio.CancelledError
        if self._event.is_set():
            return
        if self._waiting_at is None:
            self._waiting_at = time.perf_counter()
        await self._event.wait()

    def _resolve(self, *, release: bool) -> None:
        if self._resolved:
            return
        self._resolved = True
        if self._waiting_at is not None:
            self._held_ms = max(0.0, (time.perf_counter() - self._waiting_at) * 1000)
        self._rejected = not release
        if release:
            self._event.set()

    def release(self) -> None:
        self._resolve(release=True)

    def reject(self) -> None:
        self._resolve(release=False)


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
    address_check_mode: str | None = None,
    held_ms: float | None = None,
    partial_runs: int | None = None,
    partial_skip_reasons: dict | None = None,
    first_token_ms: float | None = None,
    first_tts_block_ms: float | None = None,
    first_answer_audio_ms: float | None = None,
    filler: str | None = None,
    filler_ms: float | None = None,
    turn_model_p: float | None = None,
    turn_model_ms: float | None = None,
    turn_decision: str | None = None,
    silence_used_ms: float | None = None,
    bargein: bool = False,
    bargein_ms: float | None = None,
    duck_ms: float | None = None,
    bargein_decision: str | None = None,
    speaker_score: float | None = None,
    speaker_ms: float | None = None,
    speaker_decision: str | None = None,
    address_rejected: bool = False,
) -> dict:
    model, stage = _voice_backend_details(orch)
    timing_detail = voice_timing.snapshot()
    if address_rejected:
        stage = "address_rejected"
        timing_detail["llm_calls"] = [
            call for call in timing_detail.get("llm_calls", [])
            if call.get("error") != "CancelledError"
        ]
    if partial_runs is not None:
        timing_detail["partial_runs"] = max(0, int(partial_runs))
    if partial_skip_reasons is not None:
        timing_detail["partial_skip_reasons"] = voice_timing.partial_skip_snapshot(
            partial_skip_reasons,
        )
    detail = {
        "stt_ms": stt_ms,
        "address_check_ms": address_check_ms,
        "address_check_mode": address_check_mode,
        "held_ms": held_ms,
        "first_token_ms": first_token_ms,
        "first_tts_block_ms": first_tts_block_ms,
        "first_answer_audio_ms": first_answer_audio_ms,
        "filler": filler,
        "filler_ms": filler_ms,
        "turn_model_p": turn_model_p,
        "turn_model_ms": turn_model_ms,
        "turn_decision": turn_decision,
        "silence_used_ms": silence_used_ms,
        "bargein": bool(bargein),
        "bargein_ms": bargein_ms,
        "duck_ms": duck_ms,
        "bargein_decision": bargein_decision,
        "speaker_score": speaker_score,
        "speaker_ms": speaker_ms,
        "speaker_decision": speaker_decision,
        "address_rejected": bool(address_rejected),
        "total_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "model": model,
        "stage": stage,
        **timing_detail,
    }
    log.info("voice_latency %s", detail)
    db.log_event("voice_latency", "Voice-Turn", detail)
    return detail


def _accepts_kwarg(func, name: str) -> bool:
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _call_with_turn_id(func, turn_id: str, *args):
    """Reicht die Playback-Turn-ID durch, wenn der Empfänger sie annimmt."""
    if _accepts_kwarg(func, "turn_id"):
        return func(*args, turn_id=turn_id)
    return func(*args)


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


def _is_stop_reflex(text: str) -> bool:
    """Erkennt kurze Stoppwörter auch in einem noch unvollständigen Transkript."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    if re.search(r"(?<!\w)(?:stopp|stop|halt)(?!\w)", normalized):
        return True
    if re.search(r"(?<!\w)sei\s+still(?!\w)", normalized):
        return True
    return bool(re.match(
        r"^(?:(?:mantis|mann|man|bitte)[\s,.:;-]+)*warte(?!\w)", normalized,
    ))


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
    address_check_mode: str | None = None,
    held_ms: float | None = None,
    address_metrics: dict | None = None,
    started_at: float | None = None,
    prework=None,
    partial_runs: int = 0,
    partial_skip_reasons: dict | None = None,
    partial_as_final: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    playback: _PlaybackTurn | None = None,
    execution_gate: Callable[[], Awaitable[bool]] | None = None,
    output_gate: _AudioOutputGate | None = None,
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
    synthesis_incomplete = False
    response: list[tuple[str | None, list, Exception | None]] = []
    response_trace: list = []
    next_sentence_id = 0
    ack_task: asyncio.Task | None = None
    turn_state: tts.TTSTurnState | None = None
    turn_token = None

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
        if turn_state is None or not tts._tts_turn_can_use_filler(turn_state):
            return
        selected = voice_fillers.choose_cached_filler(kind)
        if selected is None:
            return
        filler_kind = selected.kind
        pending_filler = selected
        state_changed.set()

    async def send_audio_chunk(
        audio: bytes, *, answer: bool = False, sentence: str | None = None,
        sentence_id: int | None = None, echo_text: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        nonlocal sequence, first_tts_block_ms, first_answer_audio_ms
        if output_gate is not None:
            await output_gate.wait()
        if first_tts_block_ms is None:
            first_tts_block_ms = round((time.perf_counter() - started_at) * 1000, 1)
        if answer and first_answer_audio_ms is None:
            first_answer_audio_ms = round((time.perf_counter() - started_at) * 1000, 1)
        block_id = playback.register_block(
            sequence, audio, duration_ms=duration_ms,
            text=sentence if answer else echo_text,
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
            if execution_gate is not None:
                try:
                    parameters = inspect.signature(orch.voice_respond).parameters.values()
                except (TypeError, ValueError):
                    parameters = ()
                if any(
                    parameter.name == "execution_gate"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                ):
                    respond_kwargs["execution_gate"] = execution_gate
            if _accepts_kwarg(orch.voice_respond, "voice_turn_id"):
                respond_kwargs["voice_turn_id"] = playback.turn_id
            reply, trace = await orch.voice_respond(text, **respond_kwargs)
            _mark_reflex(trace)
            reply = reply or ""
            response_trace[:] = trace or []
            response.append((reply or None, response_trace, None))
            await queue.join()
            spoken_text = _normalized_speech(" ".join(spoken_sentences))
            final_text = _normalized_speech(reply)
            if synthesis_incomplete or not final_text:
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
        nonlocal response_audio_ready, next_sentence_id, synthesis_incomplete
        try:
            while True:
                sentence = await queue.get()
                stream = None
                try:
                    if sentence is None:
                        return
                    if synthesis_incomplete:
                        continue
                    sentence_id = next_sentence_id
                    next_sentence_id += 1
                    if output_gate is not None:
                        await output_gate.wait()
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
                            synthesis_incomplete = True
                            log.error("TTS-Streaming fehlgeschlagen: %s", exc)
                            audio_queue.put_nowait((
                                "sentence_incomplete", sentence_id,
                                str(getattr(exc, "synthesized_text", "")), None,
                            ))
                            state_changed.set()
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
                    elif completed:
                        synthesis_incomplete = True
                        log.error("TTS-Streaming für einen Antwortsatz lieferte kein Audio")
                        audio_queue.put_nowait((
                            "sentence_incomplete", sentence_id, "", None,
                        ))
                        state_changed.set()
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
                await send_audio_chunk(
                    selected.audio, echo_text=selected.text,
                    duration_ms=selected.duration_ms,
                )
                if turn_state is not None:
                    turn_state.audio_emitted = True
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
                elif item[0] == "sentence_incomplete":
                    if item[1] is not None:
                        playback.mark_sentence_incomplete(item[1], item[2] or "")
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
    turn_state, turn_token = tts._start_tts_turn()
    timing = voice_timing.current()
    timing.partial_runs = max(0, int(partial_runs))
    voice_timing.set_partial_skip_reasons(partial_skip_reasons)
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
            _call_with_turn_id(recorder, playback.turn_id, playback.spoken_text())

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
                    elif synthesis_incomplete and defer_history and not playback.timed_out:
                        spoken_text = playback.spoken_text()
                        recorder = getattr(orch, "record_aborted_voice_turn", None)
                        if not callable(recorder):
                            recorder = getattr(orch, "record_interrupted_voice_turn", None)
                        if callable(recorder) and spoken_text:
                            _call_with_turn_id(recorder, playback.turn_id, spoken_text)
                    elif (
                        defer_history and not playback.timed_out
                        and response_error is None
                    ):
                        finish = getattr(orch, "finish_voice_turn", None)
                        if callable(finish) and reply:
                            _call_with_turn_id(finish, playback.turn_id, text, reply, trace)
        finally:
            try:
                _record_voice_latency(
                    orch, started_at, stt_ms=stt_ms,
                    address_check_ms=(
                        address_metrics.get("address_check_ms", address_check_ms)
                        if address_metrics is not None else address_check_ms
                    ),
                    address_check_mode=(
                        address_metrics.get("address_check_mode", address_check_mode)
                        if address_metrics is not None else address_check_mode
                    ),
                    held_ms=(output_gate.held_ms if output_gate is not None else held_ms),
                    first_token_ms=first_token_ms,
                    first_tts_block_ms=first_tts_block_ms,
                    first_answer_audio_ms=first_answer_audio_ms,
                    filler=filler_kind,
                    filler_ms=filler_ms,
                    turn_model_p=(
                        address_metrics.get("turn_model_p")
                        if address_metrics is not None else None
                    ),
                    turn_model_ms=(
                        address_metrics.get("turn_model_ms")
                        if address_metrics is not None else None
                    ),
                    turn_decision=(
                        address_metrics.get("turn_decision")
                        if address_metrics is not None else None
                    ),
                    silence_used_ms=(
                        address_metrics.get("silence_used_ms")
                        if address_metrics is not None else None
                    ),
                    bargein=playback.interrupted,
                    bargein_ms=playback.bargein_ms,
                    duck_ms=playback.current_duck_ms(),
                    bargein_decision=playback.bargein_decision,
                    speaker_score=(
                        address_metrics.get("speaker_score")
                        if address_metrics is not None else None
                    ),
                    speaker_ms=(
                        address_metrics.get("speaker_ms")
                        if address_metrics is not None else None
                    ),
                    speaker_decision=(
                        address_metrics.get("speaker_decision")
                        if address_metrics is not None else None
                    ),
                    address_rejected=bool(
                        address_metrics.get("address_rejected", False)
                        if address_metrics is not None else False
                    ),
                )
            finally:
                try:
                    if turn_token is not None:
                        tts._end_tts_turn(turn_token)
                finally:
                    voice_timing.reset(timing_token)
    if cancelled_during_playback_wait:
        raise asyncio.CancelledError
    if response_error is not None:
        raise response_error
    return reply


def build_router(orch=None) -> APIRouter:
    router = APIRouter()
    enrollment_sessions: dict[UUID, _SpeakerEnrollmentSession] = {}
    enrollment_sessions_lock = asyncio.Lock()

    async def get_enrollment_session(
        session_id: UUID, *, create: bool,
    ) -> _SpeakerEnrollmentSession | None:
        async with enrollment_sessions_lock:
            now = time.monotonic()
            expired = [
                key for key, session in enrollment_sessions.items()
                if now - session.updated_at > SPEAKER_ENROLL_SESSION_TTL_SECONDS
            ]
            for key in expired:
                enrollment_sessions.pop(key, None)

            session = enrollment_sessions.get(session_id)
            if session is None and create:
                if len(enrollment_sessions) >= SPEAKER_ENROLL_MAX_SESSIONS:
                    raise HTTPException(
                        status_code=429,
                        detail="Zu viele aktive Sprecher-Einlernsitzungen",
                    )
                session = _SpeakerEnrollmentSession()
                enrollment_sessions[session_id] = session
            return session

    async def discard_enrollment_session(
        session_id: UUID, session: _SpeakerEnrollmentSession,
    ) -> None:
        # Ein fehlgeschlagener Clip verwirft die ganze Sitzung; der Client
        # beginnt danach ohnehin mit einer neuen ID, sonst blockieren Reste
        # bis zum Ablauf die Sitzungsgrenze.
        async with session.lock:
            async with enrollment_sessions_lock:
                if enrollment_sessions.get(session_id) is session:
                    enrollment_sessions.pop(session_id, None)

    @router.get("/api/voice/speaker")
    async def speaker_status():
        from core.speaker import get_speaker_verifier

        try:
            return await get_speaker_verifier().status()
        except Exception as exc:
            log.exception("Sprecherprofilstatus konnte nicht geladen werden")
            raise HTTPException(
                status_code=503, detail="Sprecherprofil ist nicht verfügbar",
            ) from exc

    @router.post("/api/voice/speaker/enroll/clip")
    async def speaker_enroll_clip(request: Request, session_id: UUID):
        session = await get_enrollment_session(session_id, create=True)
        assert session is not None

        try:
            async with session.lock:
                async with enrollment_sessions_lock:
                    if enrollment_sessions.get(session_id) is not session:
                        raise HTTPException(
                            status_code=404, detail="Einlernsitzung ist abgelaufen",
                        )
                    session.updated_at = time.monotonic()
                    if len(session.clips) >= SPEAKER_ENROLL_MAX_CLIPS:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Maximal {SPEAKER_ENROLL_MAX_CLIPS} Clips pro Sitzung erlaubt"
                            ),
                        )
                    remaining_bytes = SPEAKER_ENROLL_MAX_BYTES - session.total_bytes
                    if remaining_bytes <= 0:
                        raise HTTPException(
                            status_code=413,
                            detail="Sprecher-Upload darf insgesamt höchstens 4 MB groß sein",
                        )

                payload = await _read_bounded_request_body(
                    request, max_bytes=remaining_bytes,
                )
                duration = _wav_duration_seconds(payload)
                if duration <= 0:
                    raise ValueError("Ungültige WAV-Datei")
                if session.total_seconds + duration > SPEAKER_ENROLL_MAX_SECONDS:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Sprecher-Upload darf insgesamt höchstens "
                            "90 Sekunden Audio enthalten"
                        ),
                    )
                session.clips.append(payload)
                session.total_bytes += len(payload)
                session.total_seconds += duration
                session.updated_at = time.monotonic()
                return {"clips": len(session.clips)}
        except HTTPException:
            await discard_enrollment_session(session_id, session)
            raise
        except ValueError as exc:
            await discard_enrollment_session(session_id, session)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/voice/speaker/enroll/finish")
    async def speaker_enroll_finish(session_id: UUID):
        session = await get_enrollment_session(session_id, create=False)
        if session is None:
            raise HTTPException(status_code=404, detail="Einlernsitzung nicht gefunden")

        async with session.lock:
            async with enrollment_sessions_lock:
                if enrollment_sessions.get(session_id) is not session:
                    raise HTTPException(
                        status_code=404, detail="Einlernsitzung ist abgelaufen",
                    )
                enrollment_sessions.pop(session_id, None)
                payloads = list(session.clips)
        if not payloads:
            raise HTTPException(status_code=400, detail="Keine Audio-Dateien hochgeladen")

        from core.speaker import get_speaker_verifier

        try:
            return await get_speaker_verifier().enroll_wavs(payloads)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("Sprecherprofil konnte nicht eingelernt werden")
            raise HTTPException(
                status_code=503, detail="Sprecherprofil konnte nicht eingelernt werden",
            ) from exc

    @router.delete("/api/voice/speaker")
    async def speaker_reset():
        from core.speaker import get_speaker_verifier

        try:
            await get_speaker_verifier().reset()
        except Exception as exc:
            log.exception("Sprecherprofil konnte nicht zurückgesetzt werden")
            raise HTTPException(
                status_code=503, detail="Sprecherprofil konnte nicht zurückgesetzt werden",
            ) from exc
        return {"enrolled": False}

    @router.post("/api/voice/segment")
    async def voice_segment(audio: UploadFile = File(...)):
        started_at = time.perf_counter()
        stt_ms = None
        address_check_ms = None
        address_check_mode = "blocking"
        first_tts_block_ms = None
        first_answer_audio_ms = None
        reply = None
        audio_b64 = None
        timing_token = None
        speaker_verifier = None
        speaker_profile_active = False
        if bool(getattr(config, "VOICE_SPEAKER_VERIFY", True)):
            try:
                from core.speaker import get_speaker_verifier

                speaker_verifier = get_speaker_verifier()
                speaker_profile_active = await speaker_verifier.has_profile()
            except Exception as exc:
                log.warning(
                    "Sprecherprofil konnte für HTTP-Voice nicht geladen werden (%s)",
                    type(exc).__name__,
                )
                speaker_profile_active = True
        try:
            suffix = Path(audio.filename or "segment.wav").suffix or ".wav"
            payload = await audio.read()
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
                tmp.write(payload)
                tmp.flush()
                stt_started = time.perf_counter()
                text, wakeword_hint = await transcribe_audio_with_hint(
                    tmp.name, wakeword_triggered=False,
                )
                stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)

            speaker_accepted = True
            if speaker_profile_active:
                speaker_accepted = False
                if speaker_verifier is not None:
                    try:
                        import numpy as np
                        from core.speaker import decode_wav

                        samples, _sample_rate = decode_wav(payload)
                        pcm_audio = np.clip(
                            np.rint(samples * 32768.0), -32768, 32767,
                        ).astype("<i2").tobytes()
                        verification = await speaker_verifier.verify_pcm(pcm_audio)
                        speaker_accepted = verification.decision == "accepted"
                    except Exception as exc:
                        log.warning(
                            "HTTP-Voice-Sprecherprüfung fehlgeschlagen (%s)",
                            type(exc).__name__,
                        )

            if text and speaker_accepted:
                address_started = time.perf_counter()
                addressed = await is_addressed_to_mantis(
                    text, wakeword_hint=wakeword_hint,
                )
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
                    address_check_mode=address_check_mode,
                    held_ms=0,
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

        speaker_verifier = None
        speaker_profile_active = False
        speaker_profile_lookup_task: asyncio.Task | None = None
        speaker_last_profile_check = 0.0
        speaker_verify_enabled = bool(getattr(config, "VOICE_SPEAKER_VERIFY", True))
        if speaker_verify_enabled:
            try:
                from core.speaker import get_speaker_verifier

                speaker_verifier = get_speaker_verifier()
                speaker_profile_active = await speaker_verifier.has_profile()
                speaker_last_profile_check = time.monotonic()
            except Exception as exc:
                log.warning(
                    "Sprecherprofil konnte für Voice-WS nicht geladen werden (%s)",
                    type(exc).__name__,
                )
                speaker_profile_active = True
        speaker_progress_contexts: dict[int, dict] = {}

        async def refresh_speaker_profile() -> None:
            nonlocal speaker_profile_active, speaker_last_profile_check
            if not speaker_verify_enabled or speaker_verifier is None:
                return
            speaker_last_profile_check = time.monotonic()
            try:
                speaker_profile_active = await speaker_verifier.has_profile()
            except Exception as exc:
                log.debug("Sprecherprofil-Refresh fehlgeschlagen (%s)", type(exc).__name__)
                speaker_profile_active = True

        def schedule_speaker_profile_refresh() -> None:
            nonlocal speaker_profile_lookup_task, speaker_last_profile_check
            if (
                not speaker_verify_enabled or speaker_verifier is None
                or speaker_profile_active
                or time.monotonic() - speaker_last_profile_check < 1.0
            ):
                return
            if speaker_profile_lookup_task is not None and not speaker_profile_lookup_task.done():
                return
            speaker_last_profile_check = time.monotonic()
            speaker_profile_lookup_task = asyncio.get_running_loop().create_task(
                refresh_speaker_profile(), name="voice-speaker-profile-refresh",
            )

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
        ducked_playback: _PlaybackTurn | None = None
        active_response_task: asyncio.Task | None = None
        background_response_tasks: set[asyncio.Task] = set()
        background_address_tasks: set[asyncio.Task] = set()
        pending_address_tasks: set[asyncio.Task] = set()
        playback_turns: dict[str, _PlaybackTurn] = {}
        recent_tts_turns: deque[_PlaybackTurn] = deque()
        conversation_window = VoiceConversationWindow()
        send_lock = asyncio.Lock()
        turn_results: asyncio.Queue[dict] = asyncio.Queue()

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

        def _address_task_done(task: asyncio.Task) -> None:
            background_address_tasks.discard(task)
            pending_address_tasks.discard(task)
            if not task.cancelled():
                try:
                    task.exception()
                except BaseException:
                    pass

        async def _cancel_pending_address_tasks() -> None:
            tasks = tuple(pending_address_tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

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

        async def duck_for_bargein(detected_at: float) -> None:
            nonlocal ducked_playback
            playback = active_playback
            if playback is None or not playback.start_duck(now=detected_at):
                return
            playback.bargein_decision = None
            ducked_playback = playback
            await send_socket.send_json({"type": "duck_playback"})

        async def unduck_playback(
            *, decision: str = "ignored", playback: _PlaybackTurn | None = None,
        ) -> None:
            nonlocal ducked_playback
            playback = playback or ducked_playback or active_playback
            if playback is None or not playback.ducked:
                return
            playback.finish_duck(now=time.monotonic(), decision=decision)
            if ducked_playback is playback:
                ducked_playback = None
            await send_socket.send_json({"type": "unduck_playback"})

        async def stop_for_bargein(
            detected_at: float, *, decision: str | None = None,
            playback: _PlaybackTurn | None = None,
        ) -> None:
            nonlocal ducked_playback
            playback = playback or active_playback
            if (
                playback is None or playback is not active_playback
                or not playback.playback_active
                or playback.interrupted
            ):
                return
            stopped_at = time.monotonic()
            playback.interrupted = True
            if not playback.progress_reported:
                playback.estimate_progress()
            conversation_window.mark_active(
                playback.assistant_text or conversation_window.address_context(),
            )
            started_at = playback.bargein_started_at or detected_at
            playback.bargein_ms = round(max(0.0, (stopped_at - started_at) * 1000), 1)
            was_ducked = playback.ducked
            playback.finish_duck(now=stopped_at, decision=decision)
            if ducked_playback is playback:
                ducked_playback = None
            _detach_active_response()
            await send_socket.send_json({
                "type": "stop_playback", "turn_id": playback.turn_id,
            })
            if was_ducked:
                await send_socket.send_json({"type": "unduck_playback"})

        async def _score_bargein_prefix(
            audio: bytes, playback: _PlaybackTurn, context: dict, *, final: bool,
            sequence: int,
        ) -> None:
            try:
                score_result = await speaker_verifier.score_pcm(audio)
                score = score_result.score
                context["speaker_ms"] = round(
                    float(context.get("speaker_ms", 0.0)) + score_result.elapsed_ms,
                    1,
                )
                if sequence <= int(context.get("decision_sequence", 0)):
                    return
                context["decision_sequence"] = sequence
                context["score"] = score
                threshold = float(getattr(config, "VOICE_SPEAKER_THRESHOLD", 0.35))
                accepted = score is not None and score >= threshold
                if final:
                    context["decision"] = (
                        "early_accept" if accepted and context.get("decision") == "early_accept"
                        else "accepted" if accepted else "rejected"
                    )
                    if accepted:
                        await stop_for_bargein(
                            time.monotonic(), decision="speaker_accepted",
                            playback=playback,
                        )
                    else:
                        if active_playback is playback:
                            await unduck_playback(
                                decision="speaker_rejected", playback=playback,
                            )
                elif accepted:
                    context["decision"] = "early_accept"
                    await stop_for_bargein(
                        time.monotonic(), decision="speaker_early_accept",
                        playback=playback,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Barge-in-Sprecherprüfung fehlgeschlagen (%s)", type(exc).__name__)
                if final and sequence > int(context.get("decision_sequence", 0)):
                    context["decision_sequence"] = sequence
                    context["decision"] = "rejected"
                    if active_playback is playback:
                        await unduck_playback(
                            decision="speaker_rejected", playback=playback,
                        )

        def handle_speech_progress(audio: bytes, speech_ms: float, generation: int) -> None:
            if not speaker_verify_enabled or speaker_verifier is None:
                return
            if not speaker_profile_active:
                schedule_speaker_profile_refresh()
                return
            playback = active_playback
            if playback is None or not playback.playback_active:
                return
            context = speaker_progress_contexts.setdefault(
                generation,
                {
                    "tasks": [], "score": None, "speaker_ms": 0.0,
                    "decision": None, "next_sequence": 0, "decision_sequence": 0,
                },
            )
            context["playback"] = playback
            if speech_ms >= 600 and not context.get("early_started"):
                prefix = bytes(audio[:int(0.6 * 16_000 * 2)])
                if len(prefix) >= 16_000:
                    context["early_started"] = True
                    context["next_sequence"] += 1
                    sequence = context["next_sequence"]
                    task = asyncio.get_running_loop().create_task(
                        _score_bargein_prefix(
                            prefix, playback, context, final=False,
                            sequence=sequence,
                        ),
                        name="voice-speaker-bargein-early",
                    )
                    context["tasks"].append(task)
            if speech_ms >= 1_000 and not context.get("final_started"):
                prefix = bytes(audio[:16_000 * 2])
                if len(prefix) >= 16_000:
                    context["final_started"] = True
                    context["next_sequence"] += 1
                    sequence = context["next_sequence"]
                    task = asyncio.get_running_loop().create_task(
                        _score_bargein_prefix(
                            prefix, playback, context, final=True,
                            sequence=sequence,
                        ),
                        name="voice-speaker-bargein-final",
                    )
                    context["tasks"].append(task)

        def handle_voice_partial(partial: str) -> None:
            if prework_cache is not None and not speaker_profile_active:
                prework_cache.offer(partial)
            playback = active_playback
            if (
                not speaker_profile_active
                and
                playback is not None and playback.playback_active
                and _is_stop_reflex(partial)
            ):
                asyncio.get_running_loop().create_task(
                    stop_for_bargein(time.monotonic(), decision="stop_word"),
                    name="voice-stop-reflex-bargein",
                )

        def conversation_active() -> bool:
            return active_playback is not None or conversation_window.is_active()

        session = VoiceStreamSession(
            vad_segmenter=segmenter,
            wakeword_detector=wakeword_detector,
            conversation_active_fn=conversation_active,
            partial_transcriber=transcribe_pcm_partial,
            partial_executor_available=partial_stt_executor_available,
            partial_interval_ms=config.VOICE_PARTIAL_STT_MS,
            on_partial=lambda partial: handle_voice_partial(partial),
            bargein_active_fn=lambda: (
                active_playback is not None and active_playback.playback_active
            ),
            on_bargein=duck_for_bargein,
            on_speech_progress=handle_speech_progress,
        )

        async def run_streaming_response(
            result: dict, text: str, prework, playback: _PlaybackTurn,
            execution_gate: Callable[[], Awaitable[bool]] | None,
            output_gate: _AudioOutputGate,
        ) -> None:
            nonlocal active_response_task, active_playback
            try:
                reply = await sende_streaming_antwort(
                    send_socket, text, orch,
                    stt_ms=result.get("stt_ms"),
                    address_check_ms=result.get("address_check_ms"),
                    address_check_mode=result.get("address_check_mode"),
                    address_metrics=result,
                    started_at=result.get("turn_started_at"),
                    prework=prework,
                    partial_runs=result.get("partial_runs", 0),
                    partial_skip_reasons=result.get("partial_skip_reasons"),
                    partial_as_final=result.get("partial_as_final", False),
                    playback=playback,
                    execution_gate=execution_gate,
                    output_gate=output_gate,
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
                if playback.ducked:
                    try:
                        await unduck_playback(
                            decision="ignored", playback=playback,
                        )
                    except (WebSocketDisconnect, RuntimeError):
                        log.debug("Voice-Playback konnte nicht zurückgesetzt werden")
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
            nonlocal active_playback, active_response_task, speaker_profile_active
            await refresh_speaker_profile()
            raw_text = str(result.get("raw_text") or result.get("text") or "").strip()
            generation = result.pop("segment_generation", None)
            speaker_context = speaker_progress_contexts.pop(generation, {})
            tasks = tuple(speaker_context.get("tasks", ()))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            pcm_audio = result.pop("pcm_audio", b"")
            result.pop("speech_pcm_audio", None)
            speaker_pcm_audio = result.pop("stt_pcm_audio", b"") or pcm_audio
            result["speaker_score"] = None
            result["speaker_ms"] = round(
                float(speaker_context.get("speaker_ms", 0.0)), 1,
            )
            result["speaker_decision"] = "no_profile"
            if speaker_profile_active:
                verification = None
                if speaker_verifier is None:
                    result["speaker_decision"] = "rejected"
                else:
                    try:
                        verification = await speaker_verifier.verify_pcm(speaker_pcm_audio)
                        result["speaker_score"] = verification.score
                        result["speaker_ms"] = round(
                            verification.elapsed_ms
                            + float(speaker_context.get("speaker_ms", 0.0)),
                            1,
                        )
                        result["speaker_decision"] = (
                            "early_accept"
                            if verification.decision == "accepted"
                            and speaker_context.get("decision") == "early_accept"
                            else verification.decision
                        )
                    except Exception as exc:
                        log.warning(
                            "Finale Sprecherprüfung fehlgeschlagen (%s)", type(exc).__name__,
                        )
                        result["speaker_decision"] = "rejected"

                if result["speaker_decision"] == "rejected":
                    await _cancel_pending_address_tasks()
                    rejected_playback = speaker_context.get("playback")
                    if rejected_playback is None and result.get("bargein"):
                        rejected_playback = ducked_playback
                    if rejected_playback is not None:
                        await unduck_playback(
                            decision="speaker_rejected", playback=rejected_playback,
                        )
                    if prework_cache is not None:
                        prework_cache.clear()
                    _record_voice_latency(
                        orch,
                        result.get("turn_started_at", time.perf_counter()),
                        stt_ms=result.get("stt_ms"),
                        partial_runs=result.get("partial_runs", 0),
                        partial_skip_reasons=result.get("partial_skip_reasons"),
                        turn_model_p=result.get("turn_model_p"),
                        turn_model_ms=result.get("turn_model_ms"),
                        turn_decision=result.get("turn_decision"),
                        silence_used_ms=result.get("silence_used_ms"),
                        speaker_score=result.get("speaker_score"),
                        speaker_ms=result.get("speaker_ms"),
                        speaker_decision="rejected",
                    )
                    return

                if (
                    speaker_verifier is not None
                    and result.get("speaker_score") is not None
                    and result["speaker_score"] >= float(
                        getattr(config, "VOICE_SPEAKER_LEARN_MIN", 0.6),
                    )
                ):
                    try:
                        await speaker_verifier.maybe_learn(
                            speaker_pcm_audio,
                            result["speaker_score"],
                            acoustic_wakeword=bool(result.get("wakeword_triggered")),
                            bargein=bool(result.get("bargein")),
                            overlapping=bool(result.get("overlapping")),
                            embedding=verification.embedding,
                        )
                    except Exception as exc:
                        log.warning(
                            "Sprecher-Mitlernen fehlgeschlagen (%s)", type(exc).__name__,
                        )
            # Raw PCM is held only through verification/optional embedding learning.
            del pcm_audio
            del speaker_pcm_audio
            # Echo-Texte stammen vom Empfangszeitpunkt (Einreihen), nicht von der
            # Abarbeitung: langsame Adress-Checks davor dürfen das Fenster nicht schließen.
            echo_texts = list(result.pop("_echo_texts", ()))
            echo_texts.extend(_recent_tts_echo_texts(time.monotonic()))
            if _is_recent_tts_echo(raw_text, echo_texts):
                log.debug("Voice-Transkript als Echo der letzten TTS-Blöcke verworfen")
                await unduck_playback(decision="ignored")
                if prework_cache is not None:
                    prework_cache.clear()
                _record_voice_latency(
                    orch,
                    result.get("turn_started_at", time.perf_counter()),
                    stt_ms=result.get("stt_ms"),
                    address_check_mode="tts_echo",
                    partial_runs=result.get("partial_runs", 0),
                    partial_skip_reasons=result.get("partial_skip_reasons"),
                    turn_model_p=result.get("turn_model_p"),
                    turn_model_ms=result.get("turn_model_ms"),
                    turn_decision=result.get("turn_decision"),
                    silence_used_ms=result.get("silence_used_ms"),
                    speaker_score=result.get("speaker_score"),
                    speaker_ms=result.get("speaker_ms"),
                    speaker_decision=result.get("speaker_decision"),
                )
                return

            # Eine neuere Äußerung ersetzt einen noch ungeklärten spekulativen Turn.
            # So können mehrere Checks nicht später in falscher Reihenfolge freigeben.
            await _cancel_pending_address_tasks()

            conversation_context = conversation_window.address_context()
            command = fast_commands.match_voice(raw_text)
            is_local_reflex = command is not None and command.tool == "voice_reflex"
            confirmation_pending = bool(
                orch is not None
                and callable(getattr(orch, "voice_confirmation_pending", None))
                and orch.voice_confirmation_pending()
            )
            stop_reflex = _is_stop_reflex(raw_text)
            playback_or_window_active = (
                active_playback is not None and active_playback.playback_active
            ) or conversation_window.is_active()
            wakeword_triggered = bool(result.get("wakeword_triggered"))
            window_reflex = (
                is_local_reflex and playback_or_window_active
                and not confirmation_pending
            )
            immediate = wakeword_triggered or window_reflex
            address_mode = "wakeword" if wakeword_triggered else (
                "window" if window_reflex else (
                    "stop_word" if stop_reflex else "speculative"
                )
            )
            result["address_check_mode"] = address_mode
            result["address_check_ms"] = None
            wakeword_hint = bool(
                result.get("wakeword_hint") or has_voice_wakeword_hint(raw_text)
            )
            text = normalize_wakeword_transcript(
                raw_text, wakeword_triggered=wakeword_triggered,
            )
            result["text"] = text

            address_decision: asyncio.Future | None = None
            execution_gate = None
            output_gate = _AudioOutputGate(open=immediate)
            if not immediate:
                address_decision = asyncio.get_running_loop().create_future()

                async def wait_for_address() -> bool:
                    return await asyncio.shield(address_decision)

                execution_gate = wait_for_address

            prework = prework_cache.take_for(text) if prework_cache is not None else None
            if prework_cache is not None:
                prework_cache.clear()

            async def call_voice_response():
                kwargs = {}
                if prework is not None:
                    kwargs["prework"] = prework
                if execution_gate is not None:
                    try:
                        parameters = inspect.signature(orch.voice_respond).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    if any(
                        parameter.name == "execution_gate"
                        or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    ):
                        kwargs["execution_gate"] = execution_gate
                return await orch.voice_respond(text, **kwargs)

            async def check_address() -> bool:
                address_kwargs = {}
                if conversation_context:
                    address_kwargs["conversation_context"] = conversation_context
                if wakeword_hint:
                    address_kwargs["wakeword_hint"] = True
                return await is_addressed_to_mantis(raw_text, **address_kwargs)

            if stop_reflex and active_playback is not None and active_playback.playback_active:
                await stop_for_bargein(
                    active_playback.bargein_started_at or time.monotonic(),
                    decision="stop_word",
                )

            if immediate:
                if active_playback is not None and active_playback.playback_active:
                    decision = (
                        "wakeword" if wakeword_triggered else
                        "stop_word" if stop_reflex else "address"
                    )
                    await stop_for_bargein(
                        active_playback.bargein_started_at or time.monotonic(),
                        decision=decision,
                    )
                _detach_active_response()
                active_playback = None

            if audio_streaming and orch is not None:
                playback = _PlaybackTurn()
                if immediate:
                    active_playback = playback
                    playback_turns[playback.turn_id] = playback
                    recent_tts_turns.append(playback)
                    active_response_task = asyncio.create_task(
                        run_streaming_response(
                            result, text, prework, playback, None, output_gate,
                        ),
                        name="voice-response-turn",
                    )
                    return

                response_task = asyncio.create_task(
                    run_streaming_response(
                        result, text, prework, playback, execution_gate, output_gate,
                    ),
                    name="voice-speculative-response-turn",
                )
                background_response_tasks.add(response_task)
                response_task.add_done_callback(_response_task_done)

                async def resolve_streaming_address() -> None:
                    nonlocal active_playback, active_response_task
                    address_started = time.perf_counter()
                    try:
                        addressed = await check_address()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug(
                            "Voice-Adresscheck fehlgeschlagen (%s)", type(exc).__name__,
                        )
                        addressed = False
                    result["address_check_ms"] = round(
                        (time.perf_counter() - address_started) * 1000, 1,
                    )
                    pending_address_tasks.discard(asyncio.current_task())
                    if not addressed:
                        result["address_rejected"] = True
                        if not address_decision.done():
                            address_decision.set_result(False)
                        output_gate.reject()
                        await unduck_playback(decision="ignored")
                        response_task.cancel()
                        await asyncio.gather(response_task, return_exceptions=True)
                        return

                    if active_playback is not None and active_playback.playback_active:
                        await stop_for_bargein(
                            active_playback.bargein_started_at or time.monotonic(),
                            decision="address",
                        )
                    _detach_active_response()
                    active_playback = playback
                    playback_turns[playback.turn_id] = playback
                    recent_tts_turns.append(playback)
                    active_response_task = response_task
                    if not address_decision.done():
                        address_decision.set_result(True)
                    output_gate.release()

                async def resolve_streaming_address_safely() -> None:
                    try:
                        await resolve_streaming_address()
                    except asyncio.CancelledError:
                        if not address_decision.done():
                            address_decision.set_result(False)
                        output_gate.reject()
                        response_task.cancel()
                        await asyncio.gather(response_task, return_exceptions=True)
                        raise

                address_task = asyncio.create_task(
                    resolve_streaming_address_safely(),
                    name="voice-address-resolution",
                )
                background_address_tasks.add(address_task)
                pending_address_tasks.add(address_task)
                address_task.add_done_callback(_address_task_done)
                return

            if not immediate and orch is None:
                started_at = result.get("turn_started_at", time.perf_counter())

                async def resolve_address_without_orchestrator() -> None:
                    address_started = time.perf_counter()
                    try:
                        addressed = await check_address()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug(
                            "Voice-Adresscheck fehlgeschlagen (%s)", type(exc).__name__,
                        )
                        addressed = False
                    result["address_check_ms"] = round(
                        (time.perf_counter() - address_started) * 1000, 1,
                    )
                    pending_address_tasks.discard(asyncio.current_task())
                    if not address_decision.done():
                        address_decision.set_result(bool(addressed))
                    if not addressed:
                        result["address_rejected"] = True
                        output_gate.reject()
                        await unduck_playback(decision="ignored")
                    _record_voice_latency(
                        orch, started_at, stt_ms=result.get("stt_ms"),
                        address_check_ms=result.get("address_check_ms"),
                        address_check_mode=address_mode, held_ms=output_gate.held_ms,
                        address_rejected=not addressed,
                        partial_runs=result.get("partial_runs", 0),
                        partial_skip_reasons=result.get("partial_skip_reasons"),
                        turn_model_p=result.get("turn_model_p"),
                        turn_model_ms=result.get("turn_model_ms"),
                        turn_decision=result.get("turn_decision"),
                        silence_used_ms=result.get("silence_used_ms"),
                        speaker_score=result.get("speaker_score"),
                        speaker_ms=result.get("speaker_ms"),
                        speaker_decision=result.get("speaker_decision"),
                    )

                address_task = asyncio.create_task(
                    resolve_address_without_orchestrator(),
                    name="voice-address-resolution",
                )
                background_address_tasks.add(address_task)
                pending_address_tasks.add(address_task)
                address_task.add_done_callback(_address_task_done)
                return

            started_at = result.get("turn_started_at", time.perf_counter())
            first_tts_block_ms = None
            first_answer_audio_ms = None
            reply = None
            timing_token = None
            if not immediate and orch is not None:
                timing_token = voice_timing.start()
                timing = voice_timing.current()
                timing.partial_runs = max(0, int(result.get("partial_runs", 0)))
                voice_timing.set_partial_skip_reasons(
                    result.get("partial_skip_reasons"),
                )
                timing.partial_as_final = bool(result.get("partial_as_final", False))
                response_task = asyncio.create_task(
                    call_voice_response(), name="voice-speculative-response",
                )
                background_response_tasks.add(response_task)
                response_task.add_done_callback(_response_task_done)

                async def resolve_nonstream_address() -> None:
                    nonlocal active_playback, active_response_task
                    local_reply = None
                    local_first_audio_ms = None
                    local_first_tts_block_ms = None
                    try:
                        address_started = time.perf_counter()
                        try:
                            addressed = await check_address()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.debug(
                                "Voice-Adresscheck fehlgeschlagen (%s)", type(exc).__name__,
                            )
                            addressed = False
                        result["address_check_ms"] = round(
                            (time.perf_counter() - address_started) * 1000, 1,
                        )
                        pending_address_tasks.discard(asyncio.current_task())
                        if not addressed:
                            result["address_rejected"] = True
                            if not address_decision.done():
                                address_decision.set_result(False)
                            output_gate.reject()
                            await unduck_playback(decision="ignored")
                            response_task.cancel()
                            await asyncio.gather(response_task, return_exceptions=True)
                            return
                        if active_playback is not None and active_playback.playback_active:
                            await stop_for_bargein(
                                active_playback.bargein_started_at or time.monotonic(),
                                decision="address",
                            )
                        _detach_active_response()
                        active_playback = None
                        if not address_decision.done():
                            address_decision.set_result(True)
                        output_gate.release()
                        local_reply, trace = await response_task
                        _mark_reflex(trace)
                        conversation_window.mark_active(local_reply)
                        await output_gate.wait()
                        local_first_audio_ms = await sende_antwort(
                            websocket, text, local_reply, audio_streaming,
                            started_at=started_at,
                        )
                        local_first_tts_block_ms = (
                            round((time.perf_counter() - started_at) * 1000, 1)
                            if local_reply else None
                        )
                    except (WebSocketDisconnect, RuntimeError) as exc:
                        log.info("Voice-WebSocket beim Antwortversand getrennt: %s", exc)
                    finally:
                        _record_voice_latency(
                            orch, started_at, stt_ms=result.get("stt_ms"),
                            address_check_ms=result.get("address_check_ms"),
                            address_check_mode=address_mode,
                            held_ms=output_gate.held_ms,
                            address_rejected=bool(result.get("address_rejected", False)),
                            first_tts_block_ms=local_first_tts_block_ms,
                            first_answer_audio_ms=local_first_audio_ms,
                            turn_model_p=result.get("turn_model_p"),
                            turn_model_ms=result.get("turn_model_ms"),
                            turn_decision=result.get("turn_decision"),
                            silence_used_ms=result.get("silence_used_ms"),
                            speaker_score=result.get("speaker_score"),
                            speaker_ms=result.get("speaker_ms"),
                            speaker_decision=result.get("speaker_decision"),
                        )

                async def resolve_nonstream_address_safely() -> None:
                    try:
                        await resolve_nonstream_address()
                    except asyncio.CancelledError:
                        if not address_decision.done():
                            address_decision.set_result(False)
                        output_gate.reject()
                        response_task.cancel()
                        await asyncio.gather(response_task, return_exceptions=True)
                        raise

                address_task = asyncio.create_task(
                    resolve_nonstream_address_safely(),
                    name="voice-address-resolution",
                )
                background_address_tasks.add(address_task)
                pending_address_tasks.add(address_task)
                address_task.add_done_callback(_address_task_done)
                voice_timing.reset(timing_token)
                return

            try:
                if orch is not None:
                    timing_token = voice_timing.start()
                    timing = voice_timing.current()
                    timing.partial_runs = max(0, int(result.get("partial_runs", 0)))
                    voice_timing.set_partial_skip_reasons(
                        result.get("partial_skip_reasons"),
                    )
                    timing.partial_as_final = bool(result.get("partial_as_final", False))
                    reply, trace = await call_voice_response()
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
                        address_check_ms=result.get("address_check_ms"),
                        address_check_mode=address_mode,
                        held_ms=output_gate.held_ms,
                        first_tts_block_ms=first_tts_block_ms,
                        first_answer_audio_ms=first_answer_audio_ms,
                        turn_model_p=result.get("turn_model_p"),
                        turn_model_ms=result.get("turn_model_ms"),
                        turn_decision=result.get("turn_decision"),
                        silence_used_ms=result.get("silence_used_ms"),
                        speaker_score=result.get("speaker_score"),
                        speaker_ms=result.get("speaker_ms"),
                        speaker_decision=result.get("speaker_decision"),
                    )
                finally:
                    if timing_token is not None:
                        voice_timing.reset(timing_token)

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
                            if playback.complete:
                                if active_playback is playback:
                                    active_playback = None
                                if playback.ducked:
                                    await unduck_playback(
                                        decision="ignored", playback=playback,
                                    )
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
                tasks_to_finish.update(background_address_tasks)
                for context in speaker_progress_contexts.values():
                    tasks_to_finish.update(context.get("tasks", ()))
                if speaker_profile_lookup_task is not None:
                    tasks_to_finish.add(speaker_profile_lookup_task)
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
