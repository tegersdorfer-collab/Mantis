import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64
import time

from fastapi.testclient import TestClient
from unittest.mock import patch
from starlette.websockets import WebSocketDisconnect
import pytest

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


def tool_start(attempt_id, name="weather"):
    return StreamEvent("tool_start", attempt_id, tool_name=name)


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

    def test_ohne_streaming_bleibt_es_beim_alten_protokoll(self, monkeypatch):
        """Alte Clients (iOS) dürfen sich nicht ändern."""
        ws = FakeWebSocket()

        def no_filler(*_args, **_kwargs):
            raise AssertionError("Nicht-streamende Clients dürfen keine Füller lesen")

        monkeypatch.setattr("core.voice_fillers.choose_cached_filler", no_filler)
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


def _fake_opus(duration_ms=1000):
    pre_skip = 312
    granule = pre_skip + int(duration_ms * 48)
    body = b"OpusHead" + bytes((1, 1)) + pre_skip.to_bytes(2, "little") + b"\0" * 7
    header = (
        b"OggS\0\0" + granule.to_bytes(8, "little") + b"\0" * 12
        + bytes((1,))
    )
    return header + bytes((len(body),)) + body


class FakeClock:
    def __init__(self):
        self.requested_delays = []
        self.sleep_started = asyncio.Event()
        self.advance_event = asyncio.Event()

    async def sleep(self, seconds):
        self.requested_delays.append(seconds)
        self.sleep_started.set()
        if seconds:
            await self.advance_event.wait()
        else:
            await asyncio.sleep(0)


def test_ack_filler_uses_fake_clock_precedes_answer_and_is_not_text(monkeypatch):
    from core.voice_fillers import VoiceFiller

    clock = FakeClock()
    release_answer = asyncio.Event()
    filler_send_started = asyncio.Event()
    release_filler_send = asyncio.Event()
    answer_tts_started = asyncio.Event()
    logged = []
    filler_audio = _fake_opus()
    chosen = []

    class FillerWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk" and base64.b64decode(data["b64"]) == filler_audio:
                filler_send_started.set()
                await release_filler_send.wait()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("ack-turn"))
            await release_answer.wait()
            await stream_cb(tool_start("ack-turn"))
            await stream_cb(delta("ack-turn", "Das ist die Antwort."))
            await stream_cb(attempt_end("ack-turn", final=True))
            return "Das ist die Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        answer_tts_started.set()
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Mhm.", filler_audio, 1000.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FillerWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Mantis, hörst du?", FakeOrchestrator(), sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        assert clock.requested_delays[0] == pytest.approx(0.8, abs=0.01)
        clock.advance_event.set()
        await filler_send_started.wait()
        release_answer.set()
        await answer_tts_started.wait()
        release_filler_send.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [message["seq"] for message in chunks] == [0, 1]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [
        filler_audio, b"answer-audio",
    ]
    assert chosen == ["ack"]
    assert ws.gesendet[-2]["reply"] == "Das ist die Antwort."
    assert "Mhm." not in ws.gesendet[-2]["reply"]
    assert logged[0][2]["filler"] == "ack"
    assert logged[0][2]["filler_ms"] == 1000.0


def test_tool_filler_is_queued_before_answer_and_only_one_filler_is_used(monkeypatch):
    from core.voice_fillers import VoiceFiller

    clock = FakeClock()
    filler_audio = _fake_opus(600)
    filler_sent = asyncio.Event()
    logged = []
    chosen = []

    class FillerWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk" and base64.b64decode(data["b64"]) == filler_audio:
                filler_sent.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("tool-turn"))
            await stream_cb(tool_start("tool-turn", "weather"))
            await filler_sent.wait()
            await stream_cb(delta("tool-turn", "Es wird sonnig."))
            await stream_cb(attempt_end("tool-turn", final=True))
            return "Es wird sonnig.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Ich schau kurz nach.", filler_audio, 600.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FillerWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Wie wird das Wetter?", FakeOrchestrator(), sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        await filler_sent.wait()
        clock.advance_event.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [message["seq"] for message in chunks] == [0, 1]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [
        filler_audio, b"answer-audio",
    ]
    assert chosen == ["tool"]
    assert logged[0][2]["filler"] == "tool"
    assert logged[0][2]["filler_ms"] == 600.0


def test_ready_answer_audio_suppresses_ack_filler(monkeypatch):
    from core.voice_fillers import VoiceFiller

    clock = FakeClock()
    choices = []
    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("fast-answer"))
            await stream_cb(delta("fast-answer", "Sofort da."))
            await stream_cb(attempt_end("fast-answer", final=True))
            return "Sofort da.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    def choose(kind):
        choices.append(kind)
        return VoiceFiller(kind, "Okay.", b"filler", 400.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FakeWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Mantis, sag etwas", FakeOrchestrator(), sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"answer-audio"]
    assert choices == []
    assert logged[0][2]["filler"] is None
    assert logged[0][2]["filler_ms"] is None


def test_voice_stream_synthesizes_first_long_clause_at_comma_without_splitting_decimal(monkeypatch):
    spoken = []
    audio_ready = asyncio.Event()

    class StreamingWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk":
                audio_ready.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("voice-comma"))
            await stream_cb(delta(
                "voice-comma",
                "Der Messwert beträgt ungefähr 10,4 Stunden, und danach ging es weiter. ",
            ))
            await audio_ready.wait()
            await stream_cb(attempt_end("voice-comma", final=True))
            return "Der Messwert beträgt ungefähr 10,4 Stunden, und danach ging es weiter.", []

    async def fake_tts(text, **_kwargs):
        spoken.append(text)
        yield f"audio-{text}".encode()

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    asyncio.run(sende_streaming_antwort(
        StreamingWebSocket(), "Mantis, lies den Messwert", FakeOrchestrator(),
    ))

    assert spoken[0] == "Der Messwert beträgt ungefähr 10,4 Stunden,"
    assert spoken[1] == "und danach ging es weiter."


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


def test_voice_latency_event_contains_phase_timings(monkeypatch):
    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            from core import voice_timing
            timing = voice_timing.current()
            timing.tool_select_ms = 12.0
            timing.context_ms = 30.0
            timing.tool_exec_ms = 5.0
            timing.llm_calls.append({
                "ms_to_first_token": 100.0, "ms_total": 200.0,
                "output_tokens": 9, "had_tool_calls": False,
            })
            return "Hallo.", []

    async def fake_tts(sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    asyncio.run(sende_streaming_antwort(FakeWebSocket(), "Hi", FakeOrchestrator(), stt_ms=1, address_check_ms=1))

    detail = logged[0][2]
    for key in ("tool_select_ms", "context_ms", "tool_exec_ms"):
        assert isinstance(detail[key], (int, float))
    assert detail["context_ms"] == 30.0
    assert detail["llm_calls"][0]["output_tokens"] == 9


def test_voice_latency_records_partial_reuse_and_saved_context_time(monkeypatch):
    from core.voice_prework import VoiceTurnPrework

    logged = []
    prework = VoiceTurnPrework(
        text="Was steht heute an",
        system="Vorgewärmter Kontext",
        keyword_tools=("get_task",),
        force_tools=False,
        elapsed_ms=64.5,
    )

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None, prework=None):
            assert prework is not None
            assert prework.system == "Vorgewärmter Kontext"
            return "Die Termine.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(),
        "Was steht heute an, bitte",
        FakeOrchestrator(),
        prework=prework,
        partial_runs=2,
    ))

    detail = logged[0][2]
    assert detail["partial_runs"] == 2
    assert detail["partial_reused"] is True
    assert detail["saved_ms"] == 64.5


def test_streaming_voice_turn_resets_timing_context(monkeypatch):
    from core import voice_timing

    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            voice_timing.current().context_ms = 17.0
            return "Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))

    async def run():
        outer_token = voice_timing.start()
        outer_timing = voice_timing.current()
        try:
            await sende_streaming_antwort(
                FakeWebSocket(), "Hi", FakeOrchestrator(),
            )
            assert voice_timing.current() is outer_timing
        finally:
            voice_timing.reset(outer_token)

    asyncio.run(run())

    assert logged[0][2]["context_ms"] == 17.0


def test_streaming_reflex_trace_sets_voice_latency_flag(monkeypatch):
    from core import voice_timing

    logged = []

    class ReflexOrchestrator:
        voice_agent = type("Agent", (), {"backend": type("Backend", (), {
            "model_name": "voice-reflex", "last_used_backend": None,
        })()})()

        async def voice_respond(self, _text, stream_cb=None):
            return "Es ist 09:07 Uhr.", [{"tool": "voice_reflex", "reflex": True}]

    async def fake_tts(_text, **_kwargs):
        yield b"REFLEX_AUDIO"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(), "Wie spät ist es?", ReflexOrchestrator(),
    ))

    assert logged[0][2]["reflex"] is True
    assert voice_timing.current() is None


def test_reflex_turn_does_not_choose_filler_while_tts_is_slow(monkeypatch):
    from core.message_handler import MessageHandler
    from core.voice_fillers import VoiceFiller

    ack_waiting = asyncio.Event()
    release_ack = asyncio.Event()
    tts_started = asyncio.Event()
    release_tts = asyncio.Event()
    chosen = []
    handler = object.__new__(MessageHandler)

    class ReflexOrchestrator:
        async def voice_respond(self, text, stream_cb=None):
            return await handler._agent_turn(text, channel_name="voice", agent=object())

    async def hold_ack(_seconds):
        ack_waiting.set()
        await release_ack.wait()

    async def slow_tts(_sentence, **_kwargs):
        tts_started.set()
        await release_tts.wait()
        yield b"reflex-answer"

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Mhm.", b"filler-audio", 500.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", slow_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FakeWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Wie spät ist es?", ReflexOrchestrator(), sleep=hold_ack,
        ))
        await ack_waiting.wait()
        await tts_started.wait()
        release_ack.set()
        # Der Ack-Timer muss seine Entscheidung treffen, während TTS noch wartet.
        await asyncio.sleep(0.01)
        release_tts.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"reflex-answer"]
    assert chosen == []


def test_disabled_fillers_still_send_audio_end(monkeypatch):
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    ws = FakeWebSocket()

    asyncio.run(sende_streaming_antwort(ws, "Sag etwas", FakeOrchestrator()))

    assert ws.gesendet[-1] == {"type": "audio_end", "count": 1}


def test_disabled_fillers_cleanup_preserves_websocket_error(monkeypatch):
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    class BrokenWebSocket(FakeWebSocket):
        async def send_json(self, data):
            if data.get("reply") == "Antwort.":
                raise RuntimeError("socket closed")
            await super().send_json(data)

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)

    with pytest.raises(RuntimeError, match="socket closed"):
        asyncio.run(sende_streaming_antwort(
            BrokenWebSocket(), "Sag etwas", FakeOrchestrator(),
        ))


def test_tool_filler_is_suppressed_after_response_audio_started(monkeypatch):
    from core.voice_fillers import VoiceFiller

    answer_audio_sent = asyncio.Event()
    filler_audio = b"tool-filler"
    chosen = []
    logged = []

    class TrackingWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk" and base64.b64decode(data["b64"]) != filler_audio:
                answer_audio_sent.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("opening"))
            await stream_cb(delta("opening", "Ich bin dran."))
            await stream_cb(attempt_end("opening", final=False))
            await answer_audio_sent.wait()
            await stream_cb(tool_start("lookup", "weather"))
            await stream_cb(attempt_start("after-tool"))
            await stream_cb(delta("after-tool", "Es wird sonnig."))
            await stream_cb(attempt_end("after-tool", final=True))
            return "Ich bin dran. Es wird sonnig.", []

    async def fake_tts(sentence, **_kwargs):
        yield f"audio:{sentence}".encode()

    async def never_sleep(_seconds):
        await asyncio.Event().wait()

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Moment.", filler_audio, 500.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)

    ws = TrackingWebSocket()
    asyncio.run(sende_streaming_antwort(
        ws, "Wie wird das Wetter?", FakeOrchestrator(), sleep=never_sleep,
    ))

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    audio = [base64.b64decode(message["b64"]) for message in chunks]
    assert audio == [b"audio:Ich bin dran.", b"audio:Es wird sonnig."]
    assert chosen == []
    assert logged[0][2]["filler"] is None


def test_turn_cancellation_cleans_up_audio_wait_helpers(monkeypatch):
    ack_waiting = asyncio.Event()
    waiting_for_ack = asyncio.Event()
    real_wait = asyncio.wait

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "Antwort ohne Audio.", []

    async def never_finish_ack(_seconds):
        ack_waiting.set()
        await asyncio.Future()

    async def no_audio_tts(_sentence, **_kwargs):
        if False:
            yield b""

    async def observe_audio_wait(tasks, **kwargs):
        tasks = tuple(tasks)
        active_names = {
            getattr(task.get_coro(), "__qualname__", "")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
        helper_names = {
            getattr(task.get_coro(), "__qualname__", "")
            for task in tasks
            if not task.done()
        }
        if (
            not any(name.endswith("produce_response") for name in active_names)
            and not any(name.endswith("synthesize_sentences") for name in active_names)
            and any(name.endswith("Queue.get") for name in helper_names)
            and any(name.endswith("Event.wait") for name in helper_names)
        ):
            waiting_for_ack.set()
        return await real_wait(tasks, **kwargs)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", no_audio_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("web.routers.voice.asyncio.wait", observe_audio_wait)

    async def run():
        turn = asyncio.create_task(sende_streaming_antwort(
            FakeWebSocket(), "Sag etwas", FakeOrchestrator(), sleep=never_finish_ack,
        ))
        await ack_waiting.wait()
        await waiting_for_ack.wait()
        helpers = {
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and getattr(task.get_coro(), "__qualname__", "").endswith(
                ("Queue.get", "Event.wait"),
            )
        }
        assert any(task.get_coro().__qualname__.endswith("Queue.get") for task in helpers)
        assert any(task.get_coro().__qualname__.endswith("Event.wait") for task in helpers)

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        leftovers = {
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
        assert leftovers == set()

    asyncio.run(run())


def test_ack_deadline_is_relative_to_turn_start_not_after_stt(monkeypatch):
    from core.voice_fillers import VoiceFiller

    clock = FakeClock()
    filler_audio = _fake_opus()
    filler_sent = asyncio.Event()
    logged = []
    chosen = []

    class FillerWebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk" and base64.b64decode(data["b64"]) == filler_audio:
                filler_sent.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("slow-stt-turn"))
            await filler_sent.wait()
            await stream_cb(delta("slow-stt-turn", "Hier bin ich."))
            await stream_cb(attempt_end("slow-stt-turn", final=True))
            return "Hier bin ich.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Mhm.", filler_audio, 700.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FillerWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Mantis, hörst du?", FakeOrchestrator(),
            started_at=time.perf_counter() - 2.5,
            sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        assert clock.requested_delays == [0]
        await task

    asyncio.run(run())

    assert chosen == ["ack"]
    assert logged[0][2]["filler"] == "ack"


def test_timer_and_first_tts_block_tie_gives_audio_priority(monkeypatch):
    from core.voice_fillers import VoiceFiller

    release_both = asyncio.Event()
    timer_waiting = asyncio.Event()
    tts_waiting = asyncio.Event()
    chosen = []
    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(attempt_start("tie-turn"))
            await stream_cb(delta("tie-turn", "Antwort."))
            await stream_cb(attempt_end("tie-turn", final=True))
            return "Antwort.", []

    async def fake_sleep(_seconds):
        timer_waiting.set()
        await release_both.wait()

    async def fake_tts(_sentence, **_kwargs):
        tts_waiting.set()
        await release_both.wait()
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        return VoiceFiller(kind, "Mhm.", b"filler-audio", 500.0)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    ws = FakeWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Sag etwas", FakeOrchestrator(), sleep=fake_sleep,
        ))
        await timer_waiting.wait()
        await tts_waiting.wait()
        # Ein gemeinsames Gate weckt Timer und ersten TTS-Block gleichzeitig.
        release_both.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"answer-audio"]
    assert chosen == []
    assert logged[0][2]["filler"] is None
