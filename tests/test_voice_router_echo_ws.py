"""WebSocket-Integration: eigenes TTS-Echo mit Weckwort darf nie einen Turn/Tool auslösen."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import json

import pytest

from test_voice_router_stream import _fake_opus

ECHO_TEXT = "Mantis, schalte das Licht aus"


def _run_scenario(monkeypatch, scenario):
    import config
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as vr

    log = []

    class FakeSession:
        def __init__(self, **kw):
            pass

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"init":
                return {"text": "Mantis, sag etwas.", "stt_ms": 1}
            if chunk == b"A":
                return {"text": "Was gibt es heute zum Essen", "stt_ms": 1}
            if chunk == b"B":
                return {"text": ECHO_TEXT, "stt_ms": 1}

        async def close(self):
            pass

    audio_sent = asyncio.Event()

    class WS:
        def __init__(self):
            self.i = 0

        async def accept(self):
            pass

        async def send_json(self, d):
            if d.get("type") == "audio_chunk":
                audio_sent.set()

        async def receive(self):
            self.i += 1
            if self.i == 1:
                return {"text": json.dumps({"type": "hello", "audio": "stream"})}
            if self.i == 2:
                return {"bytes": b"init"}
            if self.i == 3:
                await audio_sent.wait()
                await asyncio.sleep(0.3)
                return {"bytes": b"A" if scenario == "queued" else b"B"}
            if self.i == 4 and scenario == "queued":
                return {"bytes": b"B"}
            await asyncio.sleep(3.5 if scenario == "queued" else 1.0)
            return {"type": "websocket.disconnect"}

    class Orch:
        async def voice_respond(self, text, **kw):
            return "ok", []

        def record_interrupted_voice_turn(self, _t):
            pass

    async def addr(text, *, conversation_context=None):
        if text.startswith("Was"):
            await asyncio.sleep(2.5)
            return False
        return "mantis" in text.lower()

    async def resp(ws, text, orch, *, playback, **kw):
        log.append(f"TURN {text}")
        if text == "Mantis, sag etwas.":
            b = playback.register_block(
                0, _fake_opus(200), text="Mantis, schalte das Licht aus.", sentence_id=1,
            )
            playback.mark_block_sent(b)
            await ws.send_json({"type": "audio_chunk", "block_id": b, "b64": ""})
            playback.mark_audio_end(1)
            return "Mantis, schalte das Licht aus."
        log.append(f"TOOL_EXECUTED {text}")
        return "ok"

    monkeypatch.setattr(vr, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(vr, "is_addressed_to_mantis", addr)
    monkeypatch.setattr(vr, "sende_streaming_antwort", resp)
    monkeypatch.setattr(vr.db, "log_event", lambda *a: None)
    monkeypatch.setattr(vad, "SileroVAD", lambda p: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *a, **k: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda p: object())
    monkeypatch.setattr(config, "VOICE_FILLERS_ENABLED", False)
    endpoint = next(
        r.endpoint for r in vr.build_router(Orch()).routes
        if r.path == "/ws/voice/stream"
    )
    asyncio.run(asyncio.wait_for(endpoint(WS()), 15))
    return log


@pytest.mark.parametrize("scenario", ["direct", "queued"])
def test_tts_echo_with_wakeword_triggers_no_turn_or_tool(monkeypatch, scenario):
    log = _run_scenario(monkeypatch, scenario)
    assert log == ["TURN Mantis, sag etwas."]
