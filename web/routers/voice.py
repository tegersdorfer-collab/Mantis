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
import json
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, WebSocket, WebSocketDisconnect

from core import db
from core.voice import transcribe_audio, is_addressed_to_mantis, mark_conversation_active, _conversation_active
from core.voice_stream import VoiceStreamSession
from core.tts import synthesize, synthesize_stream

log = logging.getLogger("mantis.api")


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


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/voice/segment")
    async def voice_segment(audio: UploadFile = File(...)):
        suffix = Path(audio.filename or "segment.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(await audio.read())
            tmp.flush()
            text = await transcribe_audio(tmp.name)

        addressed = await is_addressed_to_mantis(text) if text else False

        reply = None
        audio_b64 = None
        if addressed and orch is not None:
            reply, _trace = await orch.voice_respond(text)
            mark_conversation_active()
            try:
                ogg = await synthesize(reply)
            except Exception as e:
                log.error(f"TTS für Voice-Antwort fehlgeschlagen: {e}")
                ogg = b""
            if ogg:
                audio_b64 = base64.b64encode(ogg).decode("ascii")

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
                reply = None
                if orch is not None:
                    reply, _trace = await orch.voice_respond(text)
                    mark_conversation_active()

                try:
                    await sende_antwort(websocket, text, reply, audio_streaming)
                except (WebSocketDisconnect, RuntimeError) as e:
                    # Client kann zwischen Antwortberechnung und send_json getrennt haben —
                    # Starlette meldet das je nach ASGI-Server als WebSocketDisconnect oder
                    # RuntimeError statt es beim nächsten receive() zu werfen.
                    log.info(f"Voice-WebSocket beim Antwortversand getrennt: {e}")
                    break
        except WebSocketDisconnect:
            log.info("Voice-WebSocket-Verbindung geschlossen")
        except Exception as e:
            log.error(f"Voice-WebSocket-Handler abgebrochen: {e}")

    return router
