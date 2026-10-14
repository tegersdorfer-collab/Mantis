import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64
import time

from fastapi.testclient import TestClient
from unittest.mock import patch
from starlette.websockets import WebSocketDisconnect

from core.stream_events import StreamEvent
from web.routers.voice import build_router, sende_antwort, sende_streaming_antwort
from fastapi import FastAPI


def make_client():
    app = FastAPI()
    app.include_router(build_router(orch=None))
    return TestClient(app)


class TestStreamModeEndpoint:
    def test_defaults_to_http(self):
        client = make_client()
        with patch("web.routers.voice.db.get_setting", return_value=None):
            resp = client.get("/api/voice/stream-mode")
        assert resp.json() == {"mode": "http"}

    def test_returns_websocket_when_set(self):
        client = make_client()
        with patch("web.routers.voice.db.get_setting", return_value="websocket"):
            resp = client.get("/api/voice/stream-mode")
        assert resp.json() == {"mode": "websocket"}



class FakeWebSocket:
    """Sammelt, was der Server sendet — ohne echten Socket, damit kein Test blockiert."""

    def __init__(self):
        self.gesendet = []

    async def send_json(self, data):
        self.gesendet.append(data)


def attempt_start(attempt_id):
    return StreamEvent("attempt_start", attempt_id)


def delta(attempt_id, text_delta):
    return StreamEvent("delta", attempt_id, text_delta=text_delta)


def attempt_end(attempt_id, final):
    return StreamEvent("attempt_end", attempt_id, final=final)


def attempt_abort(attempt_id):
    return StreamEvent("attempt_abort", attempt_id)


class TestSendeAntwort:
    """Der Ton wird blockweise geschickt, sobald er entsteht — statt erst am Ende.

    Abwärtskompatibilität ist Pflicht: iOS-Apps und /api/voice/segment kennen
    audio_chunk/audio_end nicht und müssen weiter audio_b64 bekommen.
    """

    def _stream(self, *chunks):
        async def fake(text, **kw):
            for c in chunks:
                yield c
        return patch("web.routers.voice.synthesize_stream", fake)

    def test_schickt_bloecke_einzeln(self):
        ws = FakeWebSocket()
        with self._stream(b"OGG-A", b"OGG-B"):
            asyncio.run(sende_antwort(ws, "wie ist das wetter", "Es wird warm. Und sonnig.", True))
        kopf, a, b, ende = ws.gesendet
        assert kopf["audio_streaming"] is True and kopf["audio_b64"] is None
        assert (a["type"], a["seq"], base64.b64decode(a["b64"])) == ("audio_chunk", 0, b"OGG-A")
        assert (b["seq"], base64.b64decode(b["b64"])) == (1, b"OGG-B")
        assert ende == {"type": "audio_end", "count": 2}

    def test_ohne_streaming_bleibt_es_beim_alten_protokoll(self):
        """Alte Clients (iOS) dürfen sich nicht ändern."""
        ws = FakeWebSocket()
        with patch("web.routers.voice.synthesize", return_value=b"KOMPLETT"):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", False))
        assert len(ws.gesendet) == 1
        antwort = ws.gesendet[0]
        assert base64.b64decode(antwort["audio_b64"]) == b"KOMPLETT"
        assert "audio_streaming" not in antwort

    def test_audio_end_kommt_auch_wenn_tts_scheitert(self):
        """Ohne audio_end bliebe der Client dauerhaft stummgeschaltet."""
        ws = FakeWebSocket()

        async def kaputt(text, **kw):
            raise RuntimeError("TTS tot")
            yield b""  # pragma: no cover

        with patch("web.routers.voice.synthesize_stream", kaputt):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", True))
        assert ws.gesendet[-1] == {"type": "audio_end", "count": 0}

    def test_audio_end_auch_wenn_mitten_im_stream_abbricht(self):
        """Schon gesendete Blöcke bleiben gültig, der Client wird trotzdem entsperrt."""
        ws = FakeWebSocket()

        async def haelftig(text, **kw):
            yield b"OGG-A"
            raise RuntimeError("abgebrochen")

        with patch("web.routers.voice.synthesize_stream", haelftig):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", True))
        assert ws.gesendet[-1] == {"type": "audio_end", "count": 1}

    def test_ohne_antworttext_kein_streaming(self):
        ws = FakeWebSocket()
        asyncio.run(sende_antwort(ws, "nicht adressiert", None, True))
        assert ws.gesendet == [{"text": "nicht adressiert", "addressed": True,
                                "reply": None, "audio_b64": None}]


def test_live_voice_stream_synthesizes_first_sentence_before_llm_finishes(monkeypatch):
    timeline = []
    logged = []
    audio_ready = asyncio.Event()

    class StreamingWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk":
                timeline.append(("audio", data["seq"], base64.b64decode(data["b64"])))
                audio_ready.set()

    class FakeOrchestrator:
        voice_agent = type("Agent", (), {"backend": type("Backend", (), {
            "model_name": "openai/gpt-6-luna", "last_used_backend": None,
        })()})()

        async def voice_respond(self, text, stream_cb=None):
            assert callable(stream_cb)
            await stream_cb(attempt_start("voice-1"))
            await stream_cb(delta("voice-1", "Erster Satz. "))
            await audio_ready.wait()
            timeline.append("llm-finished")
            await stream_cb(delta("voice-1", "Zweiter Satz."))
            await stream_cb(attempt_end("voice-1", final=True))
            return "Erster Satz. Zweiter Satz.", []

    async def fake_tts(sentence, **kwargs):
        timeline.append(("tts", sentence))
        yield f"audio-{sentence}".encode()

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    ws = StreamingWebSocket()

    asyncio.run(sende_streaming_antwort(
        ws, "Mantis, sag etwas", FakeOrchestrator(), stt_ms=42, address_check_ms=8,
    ))

    assert timeline.index(("audio", 0, b"audio-Erster Satz.")) < timeline.index("llm-finished")
    chunks = [msg for msg in ws.gesendet if msg.get("type") == "audio_chunk"]
    assert [msg["seq"] for msg in chunks] == [0, 1]
    assert [entry[1] for entry in timeline if isinstance(entry, tuple) and entry[0] == "tts"] == [
        "Erster Satz.", "Zweiter Satz.",
    ]
    assert ws.gesendet[-2]["reply"] == "Erster Satz. Zweiter Satz."
    assert ws.gesendet[-1] == {"type": "audio_end", "count": 2}
    segment_results = [message for message in ws.gesendet if "addressed" in message]
    assert segment_results == [ws.gesendet[-2]]
    assert logged[0][0:2] == ("voice_latency", "Voice-Turn")
    assert logged[0][2]["stt_ms"] == 42
    assert logged[0][2]["address_check_ms"] == 8
    assert logged[0][2]["first_token_ms"] is not None
    assert logged[0][2]["first_tts_block_ms"] is not None
    assert logged[0][2]["total_ms"] >= logged[0][2]["first_tts_block_ms"]
    assert logged[0][2]["model"] == "openai/gpt-6-luna"
    assert logged[0][2]["stage"] == "Backend"


def test_voice_stream_appends_real_deltas_without_splitting_words(monkeypatch):
    spoken = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("weather-1"))
            await stream_cb(delta("weather-1", "Es ist son"))
            await stream_cb(delta("weather-1", "nig."))
            await stream_cb(attempt_end("weather-1", final=True))
            return "Es ist sonnig.", []

    async def fake_tts(sentence, **_kwargs):
        spoken.append(sentence)
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    asyncio.run(sende_streaming_antwort(FakeWebSocket(), "Wetter?", FakeOrchestrator()))

    assert spoken == ["Es ist sonnig."]


def test_voice_stream_does_not_speak_an_unfinished_tail_after_length_stop(monkeypatch):
    spoken = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("voice-limit"))
            await stream_cb(delta(
                "voice-limit", "Erster Satz ist fertig. Die Nacht davor waren 8",
            ))
            await stream_cb(attempt_end("voice-limit", final=True))
            return "Erster Satz ist fertig.", []

    async def fake_tts(sentence, **_kwargs):
        spoken.append(sentence)
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(), "Wie war die Nacht?", FakeOrchestrator(),
    ))

    assert spoken == ["Erster Satz ist fertig."]


def test_voice_stream_discards_unfinished_provider_attempt_on_fallback(monkeypatch):
    spoken = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("openrouter-1"))
            await stream_cb(delta("openrouter-1", "Unfertiger Entwurf"))
            await stream_cb(attempt_abort("openrouter-1"))
            await stream_cb(attempt_start("anthropic-2"))
            await stream_cb(delta("anthropic-2", "Fertige Antwort."))
            await stream_cb(attempt_end("anthropic-2", final=True))
            return "Fertige Antwort.", []

    async def fake_tts(sentence, **_kwargs):
        spoken.append(sentence)
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    asyncio.run(sende_streaming_antwort(FakeWebSocket(), "Sag etwas", FakeOrchestrator()))

    assert spoken == ["Fertige Antwort."]


def test_voice_stream_speaks_correction_when_math_guard_changes_answer(monkeypatch):
    spoken = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("math-draft"))
            await stream_cb(delta("math-draft", "Das Ergebnis ist 4."))
            await stream_cb(attempt_end("math-draft", final=False))
            await stream_cb(attempt_start("math-retry"))
            await stream_cb(delta("math-retry", "Das Ergebnis ist 5."))
            await stream_cb(attempt_end("math-retry", final=True))
            return "Das Ergebnis ist 5.", []

    async def fake_tts(sentence, **_kwargs):
        spoken.append(sentence)
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    ws = FakeWebSocket()
    asyncio.run(sende_streaming_antwort(ws, "Rechne", FakeOrchestrator()))

    assert spoken == ["Das Ergebnis ist 4.", "Korrektur: Das Ergebnis ist 5."]
    assert ws.gesendet[-2]["reply"] == "Das Ergebnis ist 5."


def test_voice_stream_preserves_legitimate_repetition_across_attempts(monkeypatch):
    spoken = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("tool-round"))
            await stream_cb(delta("tool-round", "Los."))
            await stream_cb(attempt_end("tool-round", final=False))
            await stream_cb(attempt_start("final-round"))
            await stream_cb(delta("final-round", "Los!"))
            await stream_cb(attempt_end("final-round", final=True))
            return "Los. Los!", []

    async def fake_tts(sentence, **_kwargs):
        spoken.append(sentence)
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    asyncio.run(sende_streaming_antwort(FakeWebSocket(), "Sag los", FakeOrchestrator()))

    assert spoken == ["Los.", "Los!"]


def test_voice_stream_disconnect_cancels_agent_generation(monkeypatch):
    cancelled = asyncio.Event()
    never = asyncio.Event()

    class DisconnectedWebSocket(FakeWebSocket):
        async def send_json(self, data):
            if data.get("type") == "audio_chunk":
                raise WebSocketDisconnect(code=1000)
            await super().send_json(data)

    class SlowOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            try:
                await stream_cb(attempt_start("disconnect"))
                await stream_cb(delta("disconnect", "Ein Satz. "))
                await never.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    async def run():
        try:
            await asyncio.wait_for(
                sende_streaming_antwort(DisconnectedWebSocket(), "Mantis, antworte", SlowOrchestrator()),
                timeout=0.1,
            )
        except WebSocketDisconnect:
            return
        except TimeoutError as exc:
            raise AssertionError("Cloud-Generierung lief nach Socket-Disconnect weiter") from exc
        raise AssertionError("Sendefehler wurde verschluckt")

    asyncio.run(run())
    assert cancelled.is_set()


def test_voice_stream_latency_uses_turn_start_before_stt(monkeypatch):
    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    turn_started_at = time.perf_counter() - 0.08

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(), "Mantis", FakeOrchestrator(), stt_ms=80,
        started_at=turn_started_at,
    ))

    assert logged[0][2]["total_ms"] >= 80
