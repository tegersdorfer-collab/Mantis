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
import json
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, WebSocket, WebSocketDisconnect

import config
from core import db, voice_fillers, voice_timing
from core.voice import transcribe_audio, is_addressed_to_mantis, mark_conversation_active, _conversation_active
from core.voice_stream import VoiceStreamSession
from core.stream_events import StreamEvent
from core.tts import synthesize, synthesize_stream

log = logging.getLogger("mantis.api")


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
    filler: str | None = None,
    filler_ms: float | None = None,
) -> dict:
    model, stage = _voice_backend_details(orch)
    detail = {
        "stt_ms": stt_ms,
        "address_check_ms": address_check_ms,
        "first_token_ms": first_token_ms,
        "first_tts_block_ms": first_tts_block_ms,
        "filler": filler,
        "filler_ms": filler_ms,
        "total_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "model": model,
        "stage": stage,
        **voice_timing.snapshot(),
    }
    log.info("voice_latency %s", detail)
    db.log_event("voice_latency", "Voice-Turn", detail)
    return detail


async def sende_antwort(websocket, text: str, reply: str | None, audio_streaming: bool) -> None:
    """Schickt die Antwort — als Tonblöcke (audio_streaming) oder als ein audio_b64.

    Blockweise beginnt die Wiedergabe nach dem ersten Satz statt nach der ganzen
    Antwort (bei vier Sätzen ~0,6 s statt ~4,5 s). Der Client hält das Mikrofon
    stummgeschaltet, bis audio_end kommt — deshalb MUSS audio_end auch im
    Fehlerfall gesendet werden, sonst bleibt er dauerhaft stumm.
    """
    if not (audio_streaming and reply):
        audio_b64 = None
        if reply is not None:
            try:
                ogg = await synthesize(reply)
            except Exception as e:
                log.error(f"TTS für Voice-Antwort fehlgeschlagen: {e}")
                ogg = b""
            if ogg:
                audio_b64 = base64.b64encode(ogg).decode("ascii")
        await websocket.send_json(
            {"text": text, "addressed": True, "reply": reply, "audio_b64": audio_b64})
        return

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
        await websocket.send_json({
            "type": "audio_chunk", "seq": seq,
            "b64": base64.b64encode(ogg).decode("ascii"),
        })
        seq += 1
    await websocket.send_json({"type": "audio_end", "count": seq})


def _sentence_complete(text: str) -> bool:
    return bool(re.search(r"[.!?…][\"'»”’)}\]]*$", text.strip()))


def _normalized_speech(text: str) -> str:
    return " ".join(text.split())


async def sende_streaming_antwort(
    websocket,
    text: str,
    orch,
    *,
    stt_ms: float | None = None,
    address_check_ms: float | None = None,
    started_at: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str | None:
    """Leitet fertige Antwortsätze parallel zur laufenden Cloud-Antwort an TTS."""
    if started_at is None:
        started_at = time.perf_counter()
    first_token_ms = None
    first_tts_block_ms = None
    filler_kind: str | None = None
    filler_ms: float | None = None
    pending_filler = None
    response_audio_ready = False
    ack_done = not getattr(config, "VOICE_FILLERS_ENABLED", True)
    sequence = 0
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    state_changed = asyncio.Event()
    attempts: dict[str, dict[str, str | bool]] = {}
    has_queued_sentence = False
    spoken_sentences: list[str] = []
    response: list[tuple[str | None, Exception | None]] = []
    ack_task: asyncio.Task | None = None

    def request_filler(kind: str) -> None:
        nonlocal filler_kind, pending_filler
        if (
            not getattr(config, "VOICE_FILLERS_ENABLED", True)
            or filler_kind is not None
            or response_audio_ready
        ):
            return
        selected = voice_fillers.choose_cached_filler(kind)
        if selected is None:
            return
        filler_kind = selected.kind
        pending_filler = selected
        state_changed.set()

    async def send_audio_chunk(audio: bytes) -> None:
        nonlocal sequence, first_tts_block_ms
        if first_tts_block_ms is None:
            first_tts_block_ms = round((time.perf_counter() - started_at) * 1000, 1)
        await websocket.send_json({
            "type": "audio_chunk",
            "seq": sequence,
            "b64": base64.b64encode(audio).decode("ascii"),
        })
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
            reply, _trace = await orch.voice_respond(text, stream_cb=on_update)
            reply = reply or ""
            response.append((reply or None, None))
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
            response.append((None, exc))
            log.error("Voice-Cloud-Antwort fehlgeschlagen: %s", exc)
        finally:
            await queue.put(None)

    async def synthesize_sentences() -> None:
        nonlocal response_audio_ready
        try:
            while True:
                sentence = await queue.get()
                stream = None
                try:
                    if sentence is None:
                        return
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
                            audio_queue.put_nowait(ogg)
                            state_changed.set()
                            has_audio = True
                    if completed and has_audio:
                        spoken_sentences.append(sentence)
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
        pending_audio: bytes | None = None
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
                audio = pending_audio
                has_pending_audio = False
                pending_audio = None
                if audio is None:
                    response_finished = True
                elif pending_filler is not None:
                    pending_audio = audio
                    has_pending_audio = True
                else:
                    await send_audio_chunk(audio)
                continue

            if response_finished and ack_done:
                return

            state_changed.clear()
            audio_ready = asyncio.create_task(audio_queue.get())
            changed = asyncio.create_task(state_changed.wait())
            done, pending = await asyncio.wait(
                (audio_ready, changed), return_when=asyncio.FIRST_COMPLETED,
            )
            if audio_ready in done:
                pending_audio = audio_ready.result()
                has_pending_audio = True
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if changed in done:
                await changed

    timing_token = voice_timing.start()
    if getattr(config, "VOICE_FILLERS_ENABLED", True):
        ack_task = asyncio.create_task(send_ack_after_delay(), name="voice-filler-ack-delay")
    responder = asyncio.create_task(produce_response())
    synthesizer = asyncio.create_task(synthesize_sentences())
    sender = asyncio.create_task(send_audio())
    response_error = None
    reply = None
    client_disconnected = False
    try:
        tasks = (responder, synthesizer, sender, ack_task)
        await asyncio.gather(*(task for task in tasks if task is not None))
        reply, response_error = response[0]
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
            if not client_disconnected:
                await websocket.send_json({"type": "audio_end", "count": sequence})
        finally:
            try:
                _record_voice_latency(
                    orch, started_at, stt_ms=stt_ms,
                    address_check_ms=address_check_ms,
                    first_token_ms=first_token_ms,
                    first_tts_block_ms=first_tts_block_ms,
                    filler=filler_kind,
                    filler_ms=filler_ms,
                )
            finally:
                voice_timing.reset(timing_token)
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
                reply, _trace = await orch.voice_respond(text)
                mark_conversation_active()
                try:
                    ogg = await synthesize(reply)
                except Exception as e:
                    log.error(f"TTS für Voice-Antwort fehlgeschlagen: {e}")
                    ogg = b""
                if ogg:
                    audio_b64 = base64.b64encode(ogg).decode("ascii")
                    first_tts_block_ms = round((time.perf_counter() - started_at) * 1000, 1)
        finally:
            try:
                _record_voice_latency(
                    orch, started_at, stt_ms=stt_ms,
                    address_check_ms=address_check_ms,
                    first_tts_block_ms=first_tts_block_ms,
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
        session = VoiceStreamSession(
            vad_segmenter=segmenter,
            wakeword_detector=wakeword_detector,
            conversation_active_fn=_conversation_active,
        )

        muted = False
        # Streaming der Antwort-Audio ist OPT-IN: Der Client meldet sich nach dem
        # Connect mit {"type":"hello","audio":"stream"}. Ohne das bleibt es beim
        # bisherigen Verhalten (ein audio_b64 im Ergebnis-JSON) — die iOS-Apps und
        # der POST-Endpoint /api/voice/segment kennen das neue Protokoll nicht.
        audio_streaming = False
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
                    continue
                else:
                    continue

                if result is None:
                    continue

                text = result["text"]
                if audio_streaming and orch is not None:
                    try:
                        await sende_streaming_antwort(
                            websocket, text, orch,
                            stt_ms=result.get("stt_ms"),
                            started_at=result.get("turn_started_at"),
                        )
                        mark_conversation_active()
                    except (WebSocketDisconnect, RuntimeError) as e:
                        log.info(f"Voice-WebSocket beim Antwortversand getrennt: {e}")
                        break
                    continue

                started_at = result.get("turn_started_at", time.perf_counter())
                first_tts_block_ms = None
                reply = None
                timing_token = None
                try:
                    if orch is not None:
                        timing_token = voice_timing.start()
                        reply, _trace = await orch.voice_respond(text)
                        mark_conversation_active()
                    await sende_antwort(websocket, text, reply, audio_streaming)
                    first_tts_block_ms = (
                        round((time.perf_counter() - started_at) * 1000, 1)
                        if reply and not audio_streaming else None
                    )
                except (WebSocketDisconnect, RuntimeError) as e:
                    # Client kann zwischen Antwortberechnung und send_json getrennt haben —
                    # Starlette meldet das je nach ASGI-Server als WebSocketDisconnect oder
                    # RuntimeError statt es beim nächsten receive() zu werfen.
                    log.info(f"Voice-WebSocket beim Antwortversand getrennt: {e}")
                    break
                finally:
                    try:
                        _record_voice_latency(
                            orch, started_at, stt_ms=result.get("stt_ms"),
                            first_tts_block_ms=first_tts_block_ms,
                        )
                    finally:
                        if timing_token is not None:
                            voice_timing.reset(timing_token)
        except WebSocketDisconnect:
            log.info("Voice-WebSocket-Verbindung geschlossen")
        except Exception as e:
            log.error(f"Voice-WebSocket-Handler abgebrochen: {e}")

    return router
