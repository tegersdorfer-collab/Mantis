import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64
import inspect
import json
import time

import httpx
from fastapi.testclient import TestClient
from unittest.mock import patch
from starlette.websockets import WebSocketDisconnect
import pytest

from core.stream_events import StreamEvent
from web.routers.voice import build_router, sende_antwort, sende_streaming_antwort
from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _legacy_voice_stream_without_speaker_profile(monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_SPEAKER_VERIFY", False, raising=False)


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

    def test_bargein_history_uses_played_words_of_incomplete_sentence(self):
        from web.routers.voice import _PlaybackTurn

        playback = _PlaybackTurn()
        block_id = playback.register_block(
            0, b"audio", text="Das ist ein unvollständiger Satz.", sentence_id=0,
        )
        playback.mark_block_sent(block_id, sent_at=100.0)
        playback.record_progress(block_id, 700.0, now=100.7)

        assert playback.assistant_text == "Das ist"


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
        address_check_mode="speculative", held_ms=12.5,
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
    assert logged[0][2]["first_answer_audio_ms"] is not None
    assert logged[0][2]["total_ms"] >= logged[0][2]["first_tts_block_ms"]
    assert logged[0][2]["model"] == "openai/gpt-6-luna"
    assert logged[0][2]["stage"] == "Backend"
    assert logged[0][2]["address_check_mode"] == "speculative"
    assert logged[0][2]["held_ms"] == 12.5


def _fake_opus(duration_ms=1000):
    pre_skip = 312
    granule = pre_skip + int(duration_ms * 48)
    body = b"OpusHead" + bytes((1, 1)) + pre_skip.to_bytes(2, "little") + b"\0" * 7
    header = (
        b"OggS\0\0" + granule.to_bytes(8, "little") + b"\0" * 12
        + bytes((1,))
    )
    return header + bytes((len(body),)) + body


def test_streaming_voice_holds_tts_and_audio_until_address_gate_opens(monkeypatch):
    import web.routers.voice as voice_router

    response_started = asyncio.Event()
    release_address = asyncio.Event()
    tts_calls = []
    websocket = FakeWebSocket()

    class FakeOrchestrator:
        async def voice_respond(
            self, _text, stream_cb=None, execution_gate=None, **_kwargs,
        ):
            response_started.set()
            await stream_cb(attempt_start("answer"))
            await stream_cb(delta("answer", "Antwort."))
            await stream_cb(attempt_end("answer", final=True))
            assert await execution_gate() is True
            return "Antwort.", []

    async def fake_tts(sentence, **_kwargs):
        tts_calls.append(sentence)
        yield b"audio"

    output_gate = voice_router._AudioOutputGate()

    async def execution_gate():
        await release_address.wait()
        return True

    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            websocket, "Was ist los?", FakeOrchestrator(),
            execution_gate=execution_gate, output_gate=output_gate,
        ))
        await asyncio.wait_for(response_started.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert tts_calls == []
        assert websocket.gesendet == []
        output_gate.release()
        release_address.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert tts_calls == ["Antwort."]
    assert any(message.get("type") == "audio_chunk" for message in websocket.gesendet)
    assert output_gate.held_ms > 0


@pytest.mark.parametrize(
    ("wakeword", "authorized", "expected_mode"),
    [(True, True, "wakeword"), (False, True, "speculative"),
     (False, False, "speculative")],
)
def test_router_bypasses_real_wakeword_and_speculates_with_tool_and_audio_gates(
    monkeypatch, wakeword, authorized, expected_mode,
):
    import core.vad as vad
    import core.wakeword as wakeword_module
    import web.routers.voice as voice_router

    llm_started = asyncio.Event()
    llm_streamed = asyncio.Event()
    release_generation = asyncio.Event()
    response_cancelled = asyncio.Event()
    address_started = asyncio.Event()
    release_address = asyncio.Event()
    latency_logged = asyncio.Event()
    disconnect_allowed = asyncio.Event()
    tool_runs = []
    tts_calls = []
    logged = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"turn":
                return {
                    "text": "Erzähl mir etwas.", "wakeword_triggered": wakeword,
                    "stt_ms": 1.0, "turn_started_at": time.perf_counter(),
                }
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({"type": "hello", "audio": "stream"})}
            if self.receive_index == 2:
                return {"bytes": b"turn"}
            if self.receive_index == 3:
                await disconnect_allowed.wait()
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(
            self, _text, stream_cb=None, execution_gate=None, **_kwargs,
        ):
            llm_started.set()
            await stream_cb(attempt_start("answer"))
            await stream_cb(delta("answer", "Antwort."))
            await stream_cb(attempt_end("answer", final=True))
            llm_streamed.set()
            if execution_gate is not None:
                try:
                    await release_generation.wait()
                    if not await execution_gate():
                        raise asyncio.CancelledError
                except asyncio.CancelledError:
                    response_cancelled.set()
                    raise
            tool_runs.append("read-only-tool")
            return "Antwort.", [{"tool": "read-only-tool"}]

    async def address_check(_text, **_kwargs):
        address_started.set()
        await release_address.wait()
        return authorized

    async def fake_tts(_sentence, **_kwargs):
        tts_calls.append("answer")
        yield _fake_opus()

    def log_event(*args):
        logged.append(args)
        if args[0] == "voice_latency":
            latency_logged.set()

    async def forbidden_address_check(*_args, **_kwargs):
        raise AssertionError("akustisches openWakeWord ist bereits die Freigabe")

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(
        voice_router, "is_addressed_to_mantis",
        forbidden_address_check if wakeword else address_check,
    )
    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.db, "log_event", log_event)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword_module, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )

    async def run():
        endpoint_task = asyncio.create_task(endpoint(websocket))
        await asyncio.wait_for(
            asyncio.gather(llm_started.wait(), llm_streamed.wait()), timeout=1,
        )
        if wakeword:
            assert not address_started.is_set()
        else:
            await asyncio.wait_for(address_started.wait(), timeout=1)
            await asyncio.sleep(0.02)
            assert tool_runs == []
            assert tts_calls == []
            assert not any(item.get("type") == "audio_chunk" for item in websocket.sent)
            if authorized:
                release_generation.set()
            release_address.set()
        await asyncio.wait_for(latency_logged.wait(), timeout=1)
        detail = next(item[2] for item in logged if item[0] == "voice_latency")
        assert detail["address_check_mode"] == expected_mode
        if wakeword:
            assert detail["address_check_ms"] is None
            assert tool_runs == ["read-only-tool"]
            assert tts_calls == ["answer"]
        elif authorized:
            assert detail["address_check_ms"] is not None
            assert tool_runs == ["read-only-tool"]
            assert tts_calls == ["answer"]
            assert detail["held_ms"] > 0
        else:
            assert detail["address_check_ms"] is not None
            assert tool_runs == []
            assert response_cancelled.is_set()
            assert tts_calls == []
            assert not any(item.get("type") == "audio_chunk" for item in websocket.sent)
            assert detail["held_ms"] > 0
        disconnect_allowed.set()
        await asyncio.wait_for(endpoint_task, timeout=1)

    asyncio.run(run())


def test_conversation_window_reflex_skips_address_check_and_uses_window_mode(monkeypatch):
    import core.vad as vad
    import core.wakeword as wakeword_module
    import web.routers.voice as voice_router

    first_response_done = asyncio.Event()
    second_response_done = asyncio.Event()
    modes = []
    gates = []
    calls = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"wakeword":
                return {"text": "Mantis, sag etwas.", "wakeword_triggered": True}
            if chunk == b"reflex":
                return {"text": "Okay."}
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, _data):
            pass

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({"type": "hello", "audio": "stream"})}
            if self.receive_index == 2:
                return {"bytes": b"wakeword"}
            if self.receive_index == 3:
                await asyncio.wait_for(first_response_done.wait(), timeout=1)
                return {"bytes": b"reflex"}
            if self.receive_index == 4:
                await asyncio.wait_for(second_response_done.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, text, **_kwargs):
            calls.append(text)
            return ("Okay." if text == "Okay." else "Antwort."), []

    async def forbidden_address_check(*_args, **_kwargs):
        raise AssertionError("Wakeword und lokaler Reflex brauchen keinen Adress-Check")

    async def fake_response(_websocket, text, _orch, **kwargs):
        modes.append(kwargs["address_check_mode"])
        gates.append(kwargs.get("execution_gate"))
        reply, _trace = await _orch.voice_respond(
            text, execution_gate=kwargs.get("execution_gate"),
        )
        if text == "Okay.":
            second_response_done.set()
            return reply
        first_response_done.set()
        return reply

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", forbidden_address_check)
    monkeypatch.setattr(voice_router, "sende_streaming_antwort", fake_response)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword_module, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)

    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )
    asyncio.run(asyncio.wait_for(endpoint(FakeWebSocket()), timeout=2))

    assert calls == ["Mantis, sag etwas.", "Okay."]
    assert modes == ["wakeword", "window"]
    assert gates == [None, None]


def test_playback_turn_tracks_monotonic_progress_and_spoken_prefix():
    import web.routers.voice as voice_router

    assert hasattr(voice_router, "_PlaybackTurn")
    turn = voice_router._PlaybackTurn()
    block_id = turn.register_block(
        0, _fake_opus(2000), text="Heute wird es sonnig und warm.", sentence_id=1,
    )

    assert turn.record_progress(block_id, 900)
    assert not turn.record_progress("old-turn:0", 1500)
    assert turn.record_progress(block_id, 300)

    assert turn.progress_ms_for(block_id) == 900
    assert turn.spoken_text() == "Heute wird"


def test_playback_wait_timeout_uses_total_sent_duration_plus_two_seconds():
    import web.routers.voice as voice_router

    turn = voice_router._PlaybackTurn()
    block_id = turn.register_block(0, _fake_opus(1250), text="Antwort.", sentence_id=1)
    turn.mark_block_sent(block_id, sent_at=100.0)
    turn.mark_audio_end(1)
    assert turn._completion_timeout_s() == pytest.approx(3.25)

    turn._completion_timeout_s = lambda: 0.01
    turn._first_block_sent_at = time.monotonic()
    turn._playback_started_at = turn._first_block_sent_at

    async def wait_for_missing_progress():
        with pytest.raises(asyncio.TimeoutError):
            await turn.wait_until_complete()

    asyncio.run(wait_for_missing_progress())


def test_skipped_playback_block_is_not_saved_as_spoken_text():
    import web.routers.voice as voice_router

    turn = voice_router._PlaybackTurn()
    block_id = turn.register_block(
        0, _fake_opus(500), text="Nicht gehoert.", sentence_id=1,
    )
    turn.mark_sentence_complete(1)

    assert turn.record_progress(block_id, 0, finished=True, skipped=True)
    assert turn.spoken_text() == ""
    turn.mark_audio_end(1)
    assert turn.complete


def test_recent_tts_echo_requires_content_words_and_near_contiguous_order():
    import web.routers.voice as voice_router

    assert voice_router._is_recent_tts_echo(
        "Schalte bitte das Licht aus!", ["Schalte das Licht aus."],
    ) is True
    assert voice_router._is_recent_tts_echo(
        "Mantis, schalte das Licht aus", ["Ich schalte das Licht an."],
    ) is False
    assert voice_router._is_recent_tts_echo(
        "Licht aus", ["aus Licht"],
    ) is False
    assert voice_router._is_recent_tts_echo(
        "alpha bravo charlie delta echo foxtrot",
        ["alpha bravo xray delta echo foxtrot charlie"],
    ) is False


def test_recent_tts_echo_rejects_counter_command_with_one_differing_word():
    import web.routers.voice as voice_router

    spoken = ["Ich schalte das Licht im Wohnzimmer an und im Flur aus."]
    assert voice_router._is_recent_tts_echo(
        "Schalte das Licht im Wohnzimmer aus", spoken,
    ) is False
    assert voice_router._is_recent_tts_echo(
        "Schalte das Licht im Wohnzimmer an", spoken,
    ) is True
    assert voice_router._is_recent_tts_echo(
        "und im Flur aus", spoken,
    ) is True


def test_tts_echo_window_uses_playback_progress_instead_of_send_age():
    import web.routers.voice as voice_router

    turn = voice_router._PlaybackTurn()
    first = turn.register_block(
        0, _fake_opus(1000), text="Ich schalte das Licht an.", sentence_id=1,
    )
    second = turn.register_block(
        1, _fake_opus(500), text="Die Lampe leuchtet.", sentence_id=2,
    )
    turn.mark_block_sent(first, sent_at=100.0)
    turn.mark_block_sent(second, sent_at=100.01)

    assert turn.record_progress(first, 100, now=105.0)
    assert turn.echo_window_text(now=105.0) == (
        "Ich schalte das Licht an. Die Lampe leuchtet."
    )
    assert turn.echo_window_text(now=109.0) == ""


def test_playback_turn_marks_completed_sentence_only_after_all_audio_played():
    import web.routers.voice as voice_router

    assert hasattr(voice_router, "_PlaybackTurn")
    turn = voice_router._PlaybackTurn()
    first = turn.register_block(0, _fake_opus(500), text="Satz eins.", sentence_id=1)
    second = turn.register_block(1, _fake_opus(600), text="Satz eins.", sentence_id=1)
    turn.mark_sentence_complete(1)

    turn.record_progress(first, 500, finished=True)
    assert turn.spoken_text() == ""
    turn.record_progress(second, 600, finished=True)

    assert turn.spoken_text() == "Satz eins."


def test_streamed_voice_defers_history_until_all_playback_progress_arrives(monkeypatch):
    import web.routers.voice as voice_router

    assert "playback" in inspect.signature(sende_streaming_antwort).parameters
    audio_end_sent = asyncio.Event()
    committed = []

    class WebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_end":
                audio_end_sent.set()

    class FakeOrchestrator:
        async def voice_respond(
            self, text, stream_cb=None, prework=None, defer_finish=False,
        ):
            assert defer_finish is True
            await stream_cb(attempt_start("deferred"))
            await stream_cb(delta("deferred", "Erster Satz."))
            await stream_cb(attempt_end("deferred", final=True))
            return "Erster Satz.", [{"tool": "weather"}]

        def finish_voice_turn(self, text, reply, trace):
            committed.append((text, reply, trace))

    async def fake_tts(_sentence, **_kwargs):
        yield _fake_opus(1000)

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)

    async def run():
        websocket = WebSocket()
        playback = voice_router._PlaybackTurn()
        task = asyncio.create_task(sende_streaming_antwort(
            websocket, "Mantis, sag etwas", FakeOrchestrator(),
            playback=playback, defer_history=True,
        ))
        await asyncio.wait_for(audio_end_sent.wait(), timeout=1)

        chunks = [item for item in websocket.gesendet if item.get("type") == "audio_chunk"]
        assert len(chunks) == 1
        assert chunks[0]["block_id"]
        assert committed == []

        playback.record_progress(chunks[0]["block_id"], 1000, finished=True)
        await task

        assert committed == [(
            "Mantis, sag etwas", "Erster Satz.", [{"tool": "weather"}],
        )]

    asyncio.run(run())


def test_incomplete_synthesis_records_only_played_prefix_and_skips_full_reply(
    monkeypatch,
):
    from core.tts import TTSSynthesisIncompleteError
    import web.routers.voice as voice_router

    prefix = "Der erste Teil dieses Satzes endet hier,"
    reply = f"{prefix} und der Rest wird nicht gesprochen."
    audio_end_sent = asyncio.Event()
    recorded = []
    committed = []
    tts_calls = []

    class WebSocket(FakeWebSocket):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_end":
                audio_end_sent.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None, **_kwargs):
            await stream_cb(attempt_start("tts-incomplete"))
            await stream_cb(delta("tts-incomplete", reply))
            await stream_cb(attempt_end("tts-incomplete", final=True))
            return reply, [{"tool": "weather"}]

        def record_aborted_voice_turn(self, spoken_text):
            recorded.append(spoken_text)

        def finish_voice_turn(self, text, full_reply, trace):
            committed.append((text, full_reply, trace))

    async def broken_tts(sentence, **_kwargs):
        tts_calls.append(sentence)
        yield _fake_opus(1000)
        raise TTSSynthesisIncompleteError(
            "zweiter TTS-Teil fehlt", synthesized_text=prefix,
        )

    monkeypatch.setattr("web.routers.voice.synthesize_stream", broken_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)

    async def run():
        websocket = WebSocket()
        playback = voice_router._PlaybackTurn()
        task = asyncio.create_task(sende_streaming_antwort(
            websocket, "Mantis, erzähl etwas", FakeOrchestrator(),
            playback=playback, defer_history=True,
        ))
        await asyncio.wait_for(audio_end_sent.wait(), timeout=1)
        chunks = [
            item for item in websocket.gesendet
            if item.get("type") == "audio_chunk"
        ]
        for chunk in chunks:
            playback.record_progress(
                chunk["block_id"], 1000, finished=True,
            )
        await task
        return len(chunks), playback

    chunk_count, playback = asyncio.run(run())

    assert chunk_count == 1
    assert tts_calls == [prefix]
    assert playback.sentences[0].complete is False
    assert recorded == [prefix]
    assert committed == []


def test_websocket_bargein_stops_playback_cancels_old_turn_and_accepts_followup(
    monkeypatch,
):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    audio_sent = asyncio.Event()
    second_audio_sent = asyncio.Event()
    stop_sent = asyncio.Event()
    duck_sent = asyncio.Event()
    old_cancelled = asyncio.Event()
    new_turn_started = asyncio.Event()
    new_turn_finished = asyncio.Event()
    logged = []
    interrupted = []
    clock = lambda: time.monotonic()
    address_checks = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"first":
                return {
                    "text": "Mantis, erzähl etwas.", "stt_ms": 3.0,
                    "turn_started_at": time.perf_counter(),
                }
            if chunk == b"bargein":
                assert self.callbacks["bargein_active_fn"]() is True
                await self.callbacks["on_bargein"](clock())
                return None
            if chunk == b"final":
                assert self.callbacks["conversation_active_fn"]() is True
                return {
                    "text": "Und morgen?", "stt_ms": 4.0,
                    "turn_started_at": time.perf_counter(),
                    "wakeword_triggered": True,
                }
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_chunk":
                audio_sent.set()
                if sum(item.get("type") == "audio_chunk" for item in self.sent) > 1:
                    second_audio_sent.set()
            elif data.get("type") == "stop_playback":
                stop_sent.set()
            elif data.get("type") == "duck_playback":
                duck_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({
                    "type": "hello", "audio": "stream", "playback_progress": True,
                })}
            if self.receive_index == 2:
                return {"bytes": b"first"}
            if self.receive_index == 3:
                await asyncio.wait_for(audio_sent.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(duck_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                await asyncio.wait_for(stop_sent.wait(), timeout=1)
                block = next(item for item in self.sent if item.get("type") == "audio_chunk")
                return {"text": json.dumps({
                    "type": "playback_progress", "block_id": block["block_id"],
                        "played_ms": 600,
                })}
            if self.receive_index == 6:
                stop = next(item for item in self.sent if item.get("type") == "stop_playback")
                return {"text": json.dumps({
                    "type": "playback_stopped", "turn_id": stop["turn_id"],
                })}
            if self.receive_index == 7:
                await asyncio.wait_for(second_audio_sent.wait(), timeout=1)
                chunks = [item for item in self.sent if item.get("type") == "audio_chunk"]
                block = chunks[-1]
                return {"text": json.dumps({
                    "type": "playback_progress", "block_id": block["block_id"],
                    "played_ms": 1000, "finished": True,
                })}
            if self.receive_index == 8:
                await asyncio.wait_for(new_turn_finished.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    never = asyncio.Event()

    class FakeOrchestrator:
        def __init__(self):
            self.calls = []

        async def voice_respond(
            self, text, stream_cb=None, prework=None, defer_finish=False,
        ):
            self.calls.append((text, defer_finish))
            if len(self.calls) == 1:
                await stream_cb(attempt_start("interrupted-attempt"))
                await stream_cb(delta("interrupted-attempt", "Erster Satz."))
                await stream_cb(attempt_end("interrupted-attempt", final=False))
                try:
                    await never.wait()
                except asyncio.CancelledError:
                    await stream_cb(attempt_abort("interrupted-attempt"))
                    old_cancelled.set()
                    raise
            new_turn_started.set()
            return "Morgen wird es sonnig.", []

        def record_interrupted_voice_turn(self, spoken_text):
            interrupted.append(spoken_text)

        def finish_voice_turn(self, text, reply, trace):
            new_turn_finished.set()

    async def fake_tts(_sentence, **_kwargs):
        yield _fake_opus(1000)

    async def addressed(_text, *, conversation_context=None):
        address_checks.append(_text)
        return True

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", addressed)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.db, "log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    orchestrator = FakeOrchestrator()
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(orchestrator).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=3))

    assert stop_sent.is_set()
    assert old_cancelled.is_set()
    assert orchestrator.calls == [
        ("Mantis, erzähl etwas.", True), ("Mantis, Und morgen?", True),
    ]
    assert interrupted == ["Erster"]
    assert address_checks == ["Mantis, erzähl etwas."]
    assert any(item.get("type") == "stop_playback" for item in websocket.sent)
    old_latency = next(event[2] for event in logged if event[2].get("bargein") is True)
    assert old_latency["bargein_ms"] >= 0
    assert old_latency["duck_ms"] >= 0
    assert old_latency["bargein_decision"] == "wakeword"


@pytest.mark.parametrize(
    ("transcript", "reply", "addressed", "forwarded"),
    [
        ("Licht aus", "Licht aus.", False, False),
        ("Licht aus", "Das Licht ist an.", False, False),
        ("Mantis, das Licht aus", "das Licht aus.", False, False),
        ("Schalte das Licht aus", "Ich schalte das Licht an.", True, True),
        ("Schalte das Licht an", "Ich schalte das Licht aus.", True, True),
        ("Stopp, mach lieber das Licht aus", "Die Lampe bleibt an.", True, True),
    ],
    ids=[
        "tts-echo", "room-speech", "tts-echo-prefixed", "opposite-command-aus", "opposite-command-an",
        "addressed-interruption",
    ],
)
def test_bargein_segment_requires_non_echo_and_address_check(
    monkeypatch, transcript, reply, addressed, forwarded,
):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    first_audio_sent = asyncio.Event()
    first_stop_sent = asyncio.Event()
    duck_sent = asyncio.Event()
    unduck_sent = asyncio.Event()
    second_audio_end_sent = asyncio.Event()
    second_turn_finished = asyncio.Event()
    second_address_check_finished = asyncio.Event()
    unexpected_tool_started = asyncio.Event()
    address_checks = []
    logged = []
    prefixed_echo = transcript == "Mantis, das Licht aus" and reply == "das Licht aus."
    expect_address_check = not (
        (transcript == "Licht aus" and reply == "Licht aus.") or prefixed_echo
    )

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {"text": "Mantis, sag etwas.", "stt_ms": 1.0}
            if chunk == b"bargein":
                assert self.callbacks["bargein_active_fn"]() is True
                await self.callbacks["on_bargein"](time.monotonic())
                return None
            if chunk == b"final":
                result = {"text": transcript, "stt_ms": 1.0, "bargein": True}
                if prefixed_echo:
                    result.update(
                        raw_text="das Licht aus", wakeword_triggered=True,
                    )
                return result
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_chunk":
                chunks = [item for item in self.sent if item.get("type") == "audio_chunk"]
                if len(chunks) == 1:
                    first_audio_sent.set()
            elif data.get("type") == "audio_end":
                if len([item for item in self.sent if item.get("type") == "audio_end"]) == 2:
                    second_audio_end_sent.set()
            elif data.get("type") == "stop_playback":
                first_stop_sent.set()
            elif data.get("type") == "duck_playback":
                duck_sent.set()
            elif data.get("type") == "unduck_playback":
                unduck_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({
                    "type": "hello", "audio": "stream", "playback_progress": True,
                })}
            if self.receive_index == 2:
                return {"bytes": b"initial"}
            if self.receive_index == 3:
                await asyncio.wait_for(first_audio_sent.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(duck_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                if forwarded:
                    await asyncio.wait_for(first_stop_sent.wait(), timeout=1)
                    stop = next(item for item in self.sent if item.get("type") == "stop_playback")
                    return {"text": json.dumps({
                        "type": "playback_stopped", "turn_id": stop["turn_id"],
                    })}
                if expect_address_check:
                    await asyncio.wait_for(second_address_check_finished.wait(), timeout=1)
                await asyncio.wait_for(unduck_sent.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            if forwarded and self.receive_index == 6:
                await asyncio.wait_for(second_audio_end_sent.wait(), timeout=1)
                chunks = [item for item in self.sent if item.get("type") == "audio_chunk"]
                return {"text": json.dumps({
                    "type": "playback_progress", "block_id": chunks[-1]["block_id"],
                    "played_ms": 1000, "finished": True,
                })}
            if forwarded and self.receive_index == 7:
                await asyncio.wait_for(second_turn_finished.wait(), timeout=1)
            elif not forwarded:
                raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")
            return {"type": "websocket.disconnect"}

    class FakeOrchestrator:
        def __init__(self):
            self.calls = []

        async def voice_respond(self, text, **kwargs):
            execution_gate = kwargs.get("execution_gate")
            if execution_gate is not None and not await execution_gate():
                raise asyncio.CancelledError
            self.calls.append(text)
            if len(self.calls) > 1:
                unexpected_tool_started.set()
            if len(self.calls) == 1:
                return reply, []
            return "Befehl ausgeführt.", []

        def finish_voice_turn(self, _text, _reply, _trace):
            second_turn_finished.set()

        def record_interrupted_voice_turn(self, _spoken_text):
            pass

    async def address_check(text, *, conversation_context=None):
        address_checks.append((text, conversation_context))
        if text.startswith("Mantis,"):
            return True
        result = addressed
        second_address_check_finished.set()
        return result

    async def fake_tts(_sentence, **_kwargs):
        yield _fake_opus(1000)

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.db, "log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    orchestrator = FakeOrchestrator()
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(orchestrator).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=3))

    assert orchestrator.calls == (
        ["Mantis, sag etwas.", transcript] if forwarded else ["Mantis, sag etwas."]
    )
    if (transcript == "Licht aus" and reply == "Licht aus.") or prefixed_echo:
        assert [text for text, _context in address_checks] == ["Mantis, sag etwas."]
    if prefixed_echo:
        assert not unexpected_tool_started.is_set()
    elif not (transcript == "Licht aus" and reply == "Licht aus."):
        assert address_checks[-1] == (transcript, reply)
    if not forwarded and expect_address_check:
        rejected = [
            event[2] for event in logged
            if event[0] == "voice_latency" and event[2].get("address_rejected")
        ]
        assert rejected
        assert rejected[-1]["stage"] == "address_rejected"
        assert all(
            call.get("error") != "CancelledError"
            for call in rejected[-1]["llm_calls"]
        )


def test_receive_loop_does_not_wait_for_cancelled_response_before_control_messages(
    monkeypatch,
):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    response_started = asyncio.Event()
    response_cancelled = asyncio.Event()
    release_cancelled_response = asyncio.Event()
    stop_sent = asyncio.Event()
    duck_sent = asyncio.Event()
    second_response_started = asyncio.Event()
    first_block_id = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {"text": "Mantis, sag etwas.", "stt_ms": 1.0}
            if chunk == b"bargein":
                await self.callbacks["on_bargein"](time.monotonic())
                return None
            if chunk == b"final":
                return {"text": "Stopp, mach lieber das Licht aus", "stt_ms": 1.0}
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            if data.get("type") == "stop_playback":
                await asyncio.sleep(0)
                assert response_cancelled.is_set(), "stop_playback muss nach cancel gesendet werden"
                stop_sent.set()
            elif data.get("type") == "duck_playback":
                duck_sent.set()
            self.sent.append(data)

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({
                    "type": "hello", "audio": "stream", "playback_progress": True,
                })}
            if self.receive_index == 2:
                return {"bytes": b"initial"}
            if self.receive_index == 3:
                await asyncio.wait_for(response_started.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(duck_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                await asyncio.wait_for(stop_sent.wait(), timeout=1)
                release_cancelled_response.set()
                return {"text": json.dumps({
                    "type": "playback_progress", "block_id": first_block_id[0],
                    "played_ms": 400,
                })}
            if self.receive_index == 6:
                stop = next(item for item in self.sent if item.get("type") == "stop_playback")
                return {"text": json.dumps({
                    "type": "playback_stopped", "turn_id": stop["turn_id"],
                })}
            if self.receive_index == 7:
                await asyncio.wait_for(second_response_started.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, _text, **_kwargs):
            return "Antwort.", []

    async def slow_response(_websocket, _text, _orch, *, playback, **_kwargs):
        if not response_started.is_set():
            first_block_id.append(playback.register_block(
                0, _fake_opus(1000), text="Antwort.", sentence_id=1,
            ))
            response_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                response_cancelled.set()
                await release_cancelled_response.wait()
                raise
        second_response_started.set()
        return "Antwort."

    async def addressed(_text, *, conversation_context=None):
        return True

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", addressed)
    monkeypatch.setattr(voice_router, "sende_streaming_antwort", slow_response)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )

    async def run():
        try:
            await asyncio.wait_for(endpoint(websocket), timeout=1.5)
        except asyncio.TimeoutError as exc:
            raise AssertionError("Receive-Schleife wartete auf die alte Antworttask") from exc

    asyncio.run(run())
    assert response_cancelled.is_set()
    assert release_cancelled_response.is_set()
    assert second_response_started.is_set()


@pytest.mark.parametrize("addressed", [True, False], ids=["addressed", "not-addressed"])
def test_bargein_ducks_until_address_check_and_receive_loop_stays_live(
    monkeypatch, addressed,
):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    first_audio_sent = asyncio.Event()
    response_cancelled = asyncio.Event()
    stop_sent = asyncio.Event()
    duck_sent = asyncio.Event()
    unduck_sent = asyncio.Event()
    address_started = asyncio.Event()
    address_finished = asyncio.Event()
    release_address_check = asyncio.Event()
    probe_processed = asyncio.Event()
    second_turn_started = asyncio.Event()
    allow_disconnect = asyncio.Event()
    response_texts = []
    playbacks = []
    orchestrator_calls = []
    tool_runs = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {"text": "Mantis, sag etwas.", "stt_ms": 1.0}
            if chunk == b"bargein":
                await self.callbacks["on_bargein"](time.monotonic())
            elif chunk == b"final":
                return {"text": "Schalte das Licht aus", "stt_ms": 1.0}
            elif chunk == b"probe":
                probe_processed.set()
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_chunk":
                first_audio_sent.set()
            elif data.get("type") == "stop_playback":
                stop_sent.set()
            elif data.get("type") == "duck_playback":
                duck_sent.set()
            elif data.get("type") == "unduck_playback":
                unduck_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({
                    "type": "hello", "audio": "stream", "playback_progress": True,
                })}
            if self.receive_index == 2:
                return {"bytes": b"initial"}
            if self.receive_index == 3:
                await asyncio.wait_for(first_audio_sent.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(duck_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                await asyncio.wait_for(address_started.wait(), timeout=1)
                return {"bytes": b"probe"}
            if self.receive_index == 6:
                await asyncio.wait_for(allow_disconnect.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, text, **kwargs):
            execution_gate = kwargs.get("execution_gate")
            if execution_gate is not None and not await execution_gate():
                raise asyncio.CancelledError
            orchestrator_calls.append(text)
            tool_runs.append(text)
            return "Befehl ausgeführt.", []

        def record_interrupted_voice_turn(self, _spoken_text):
            pass

    async def address_check(text, *, conversation_context=None):
        if text == "Mantis, sag etwas.":
            return True
        address_started.set()
        await release_address_check.wait()
        address_finished.set()
        return addressed

    async def streaming_response(websocket, text, _orch, *, playback, **kwargs):
        response_texts.append(text)
        playbacks.append(playback)
        if len(response_texts) == 1:
            block_id = playback.register_block(
                0, _fake_opus(1000), text="Antwort.", sentence_id=1,
            )
            playback.mark_block_sent(block_id)
            await websocket.send_json({
                "type": "audio_chunk", "block_id": block_id, "b64": "",
            })
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                response_cancelled.set()
                raise
        execution_gate = kwargs.get("execution_gate")
        if execution_gate is not None and not await execution_gate():
            return None
        second_turn_started.set()
        await _orch.voice_respond(text, execution_gate=execution_gate)
        return "Befehl ausgeführt."

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    monkeypatch.setattr(voice_router, "sende_streaming_antwort", streaming_response)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )

    async def run():
        endpoint_task = asyncio.create_task(endpoint(websocket))
        try:
            await asyncio.wait_for(probe_processed.wait(), timeout=1)
            assert address_started.is_set()
            assert not address_finished.is_set()
            assert duck_sent.is_set()
            assert not stop_sent.is_set()
            assert not response_cancelled.is_set()
            assert response_texts == ["Mantis, sag etwas.", "Schalte das Licht aus"]
            assert not second_turn_started.is_set()
            assert orchestrator_calls == []
            assert tool_runs == []

            release_address_check.set()
            if addressed:
                await asyncio.wait_for(second_turn_started.wait(), timeout=1)
                assert stop_sent.is_set()
                assert response_cancelled.is_set()
                assert playbacks[0].bargein_decision == "address"
                assert playbacks[0].duck_ms >= 0
                assert response_texts == ["Mantis, sag etwas.", "Schalte das Licht aus"]
                assert orchestrator_calls == ["Schalte das Licht aus"]
            else:
                await asyncio.wait_for(unduck_sent.wait(), timeout=1)
                assert not stop_sent.is_set()
                assert not response_cancelled.is_set()
                assert playbacks[0].bargein_decision == "ignored"
                assert playbacks[0].duck_ms >= 0
                assert not second_turn_started.is_set()
                assert response_texts == ["Mantis, sag etwas.", "Schalte das Licht aus"]
                assert orchestrator_calls == []
                assert tool_runs == []
            allow_disconnect.set()
            await asyncio.wait_for(endpoint_task, timeout=1)
        finally:
            release_address_check.set()
            allow_disconnect.set()
            if not endpoint_task.done():
                endpoint_task.cancel()
            await asyncio.gather(endpoint_task, return_exceptions=True)

    asyncio.run(run())


def test_unaddressed_speech_during_playback_is_ignored_and_records_duck_latency(
    monkeypatch,
):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    audio_end_sent = asyncio.Event()
    duck_sent = asyncio.Event()
    unduck_sent = asyncio.Event()
    interrupted_texts = []
    logged = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {"text": "Mantis, sag etwas.", "stt_ms": 1.0}
            if chunk == b"bargein":
                await self.callbacks["on_bargein"](time.monotonic())
                return None
            if chunk == b"final":
                return {"text": "Ich spreche mit jemand anderem", "stt_ms": 1.0}
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_end":
                audio_end_sent.set()
            elif data.get("type") == "duck_playback":
                duck_sent.set()
            elif data.get("type") == "unduck_playback":
                unduck_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({
                    "type": "hello", "audio": "stream", "playback_progress": True,
                })}
            if self.receive_index == 2:
                return {"bytes": b"initial"}
            if self.receive_index == 3:
                await asyncio.wait_for(audio_end_sent.wait(), timeout=1)
                await asyncio.sleep(1.05)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(duck_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                await asyncio.wait_for(unduck_sent.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, _text, **_kwargs):
            return "Erster Satz.", []

        def record_interrupted_voice_turn(self, spoken_text):
            interrupted_texts.append(spoken_text)

    async def address_check(text, *, conversation_context=None):
        return text.startswith("Mantis,")

    async def fake_tts(_sentence, **_kwargs):
        yield _fake_opus(1000)

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.db, "log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=3))

    assert interrupted_texts == []
    latency = next(event[2] for event in logged if event[0] == "voice_latency")
    assert latency["bargein"] is False
    assert latency["bargein_decision"] == "ignored"
    assert latency["duck_ms"] is not None
    assert latency["duck_ms"] >= 0


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


def test_ack_filler_is_suppressed_for_fallback_voice(monkeypatch):
    from core import tts

    clock = FakeClock()
    release_answer = asyncio.Event()
    answer_tts_started = asyncio.Event()
    release_answer_tts = asyncio.Event()
    logged = []
    chosen = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            tts._active_tts_turn.get().engine = "google"
            await stream_cb(attempt_start("ack-turn"))
            await stream_cb(delta("ack-turn", "Das ist die Antwort. "))
            await release_answer.wait()
            await stream_cb(tool_start("ack-turn"))
            await stream_cb(attempt_end("ack-turn", final=True))
            return "Das ist die Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        answer_tts_started.set()
        await release_answer_tts.wait()
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        raise AssertionError("Fallback-Stimme darf keinen Hauptstimmen-Füller auswählen")

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google", "local"])
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True)
    ws = FakeWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Mantis, hörst du?", FakeOrchestrator(), sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        assert clock.requested_delays[0] == pytest.approx(0.8, abs=0.01)
        await answer_tts_started.wait()
        clock.advance_event.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release_answer_tts.set()
        release_answer.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [message["seq"] for message in chunks] == [0]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"answer-audio"]
    assert chosen == []
    assert ws.gesendet[-2]["reply"] == "Das ist die Antwort."
    assert logged[0][2]["filler"] is None
    assert logged[0][2]["filler_ms"] is None
    assert logged[0][2]["first_answer_audio_ms"] is not None


def test_ack_filler_uses_preselected_main_voice_before_answer_audio(monkeypatch):
    from core import tts
    from core.voice_fillers import VoiceFiller

    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google"], raising=False)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    monkeypatch.setattr("config.CARTESIA_API_KEY", "cartesia-test-key", raising=False)
    monkeypatch.setattr("config.GOOGLE_TTS_API_KEY", "google-test-key", raising=False)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 0)
    monkeypatch.setattr(tts, "_engine_paused_until", {})
    monkeypatch.setattr(tts, "_cartesia_paused_until", 0.0)
    monkeypatch.setattr(tts, "_google_tts_paused_until", 0.0)
    selected = []

    def choose(kind):
        selected.append(kind)
        return VoiceFiller(kind, "Mhm.", b"ack-audio", 350.0)

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "", []

    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    ws = FakeWebSocket()

    asyncio.run(sende_streaming_antwort(ws, "Mantis?", FakeOrchestrator()))

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"ack-audio"]
    assert selected == ["ack"]


def test_gesendeter_filler_bindet_tts_turn_fuer_retry(monkeypatch):
    from core import tts
    from core.voice_fillers import VoiceFiller

    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google"], raising=False)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    monkeypatch.setattr("config.CARTESIA_API_KEY", "cartesia-test-key", raising=False)
    monkeypatch.setattr("config.GOOGLE_TTS_API_KEY", "google-test-key", raising=False)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 0)
    monkeypatch.setattr(tts, "_engine_paused_until", {})
    monkeypatch.setattr(tts, "_engine_error_paused_until", {}, raising=False)
    monkeypatch.setattr(tts, "_engine_quota_paused_until", {}, raising=False)
    monkeypatch.setattr(tts, "_engine_pause_warned", set())
    monkeypatch.setattr(tts, "_cartesia_paused_until", 0.0)
    monkeypatch.setattr(tts, "_google_tts_paused_until", 0.0)
    monkeypatch.setattr(tts, "_cartesia_pause_warned", False)
    monkeypatch.setattr(tts, "_google_tts_pause_warned", False)
    monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)
    monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)
    monkeypatch.setattr(tts, "_cartesia_wav_to_ogg", lambda _wav: b"OggS:cartesia")
    monkeypatch.setattr(tts, "_ogg_opus_sample_count", lambda _audio: 4800)
    monkeypatch.setattr(
        "core.voice_fillers.choose_cached_filler",
        lambda kind: VoiceFiller(kind, "Mhm.", b"filler-audio", 350.0),
    )
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    cartesia_calls = []
    google_calls = []

    async def cartesia(_text):
        cartesia_calls.append(True)
        if len(cartesia_calls) == 1:
            raise httpx.ReadTimeout("kurzer Fehler")
        return b"WAV:cartesia"

    async def google(_text):
        google_calls.append(True)
        return b"OggS:google", False

    monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
    monkeypatch.setattr(tts, "_google_tts_synthesize", google)

    class FillerAwareWebSocket(FakeWebSocket):
        def __init__(self):
            super().__init__()
            self.filler_sent = asyncio.Event()

        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "audio_chunk" and base64.b64decode(data["b64"]) == b"filler-audio":
                self.filler_sent.set()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await ws.filler_sent.wait()
            return "Antwort.", []

    ws = FillerAwareWebSocket()
    asyncio.run(sende_streaming_antwort(ws, "Mantis?", FakeOrchestrator()))

    chunks = [
        base64.b64decode(message["b64"])
        for message in ws.gesendet
        if message.get("type") == "audio_chunk"
    ]
    assert chunks == [b"filler-audio", b"OggS:cartesia"]
    assert len(cartesia_calls) == 2
    assert google_calls == []


def test_tool_filler_uses_preselected_main_voice_before_answer_audio(monkeypatch):
    from core import tts
    from core.voice_fillers import VoiceFiller

    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google"], raising=False)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    monkeypatch.setattr("config.CARTESIA_API_KEY", "cartesia-test-key", raising=False)
    monkeypatch.setattr("config.GOOGLE_TTS_API_KEY", "google-test-key", raising=False)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 0)
    monkeypatch.setattr(tts, "_engine_paused_until", {})
    monkeypatch.setattr(tts, "_cartesia_paused_until", 0.0)
    monkeypatch.setattr(tts, "_google_tts_paused_until", 0.0)
    selected = []

    def choose(kind):
        selected.append(kind)
        if kind == "ack":
            return None
        return VoiceFiller(kind, "Ich schau nach.", b"tool-audio", 500.0)

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await stream_cb(tool_start("tool-only", "weather"))
            return "", []

    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    ws = FakeWebSocket()

    asyncio.run(sende_streaming_antwort(ws, "Wie wird das Wetter?", FakeOrchestrator()))

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"tool-audio"]
    assert "tool" in selected


def test_main_voice_cooldown_preselects_fallback_and_suppresses_ack_filler(monkeypatch):
    from core import tts

    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google"], raising=False)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    monkeypatch.setattr("config.CARTESIA_API_KEY", "cartesia-test-key", raising=False)
    monkeypatch.setattr("config.GOOGLE_TTS_API_KEY", "google-test-key", raising=False)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 0)
    monkeypatch.setattr(tts.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(tts, "_engine_paused_until", {"cartesia": 200.0})
    monkeypatch.setattr(tts, "_cartesia_paused_until", 200.0)
    monkeypatch.setattr(tts, "_google_tts_paused_until", 0.0)
    preselected = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            preselected.append(tts._active_tts_turn.get().engine)
            return "", []

    def choose(_kind):
        raise AssertionError("Bei vorausgewählter Ersatzstimme darf kein Füller laufen")

    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *_args: None)
    ws = FakeWebSocket()

    asyncio.run(sende_streaming_antwort(ws, "Mantis?", FakeOrchestrator()))

    assert preselected == ["google"]
    assert not [message for message in ws.gesendet if message.get("type") == "audio_chunk"]


def test_tool_filler_is_suppressed_when_fallback_voice_is_selected(monkeypatch):
    from core import tts

    clock = FakeClock()
    logged = []
    chosen = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            tts._active_tts_turn.get().engine = "google"
            await stream_cb(attempt_start("tool-turn"))
            await stream_cb(tool_start("tool-turn", "weather"))
            await stream_cb(delta("tool-turn", "Es wird sonnig."))
            await stream_cb(attempt_end("tool-turn", final=True))
            return "Es wird sonnig.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        raise AssertionError("Fallback-Stimme darf keinen Hauptstimmen-Füller auswählen")

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google", "local"])
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True)
    ws = FakeWebSocket()

    async def run():
        task = asyncio.create_task(sende_streaming_antwort(
            ws, "Wie wird das Wetter?", FakeOrchestrator(), sleep=clock.sleep,
        ))
        await clock.sleep_started.wait()
        clock.advance_event.set()
        await task

    asyncio.run(run())

    chunks = [message for message in ws.gesendet if message.get("type") == "audio_chunk"]
    assert [message["seq"] for message in chunks] == [0]
    assert [base64.b64decode(message["b64"]) for message in chunks] == [b"answer-audio"]
    assert chosen == []
    assert logged[0][2]["filler"] is None
    assert logged[0][2]["filler_ms"] is None


def test_foreign_speaker_wakeword_is_rejected_before_address_or_tools(monkeypatch):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    verification_finished = asyncio.Event()
    address_checks = []
    assistant_calls = []
    logged = []

    class FakeSpeakerVerifier:
        async def has_profile(self):
            return True

        async def verify_pcm(self, audio):
            assert audio == b"pcm-audio"
            verification_finished.set()
            return type(
                "Verification", (),
                {"score": 0.21, "elapsed_ms": 7.5, "decision": "rejected"},
            )()

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def handle_chunk(self, _chunk, muted=False):
            return {
                "text": "Mantis, lösche alle Dateien",
                "raw_text": "Mantis, lösche alle Dateien",
                "wakeword_triggered": True,
                "pcm_audio": b"pcm-audio",
                "stt_ms": 2.0,
                "turn_started_at": time.perf_counter(),
            }

        async def close(self):
            pass

    class FakeWebSocket:
        async def accept(self):
            pass

        async def send_json(self, _data):
            raise AssertionError("abgewiesener Sprecher darf keine Antwort senden")

        async def receive(self):
            if not hasattr(self, "received"):
                self.received = True
                return {"bytes": b"final"}
            await asyncio.wait_for(verification_finished.wait(), timeout=1)
            await asyncio.sleep(0.02)
            return {"type": "websocket.disconnect"}

    class FakeOrchestrator:
        async def voice_respond(self, *args, **_kwargs):
            assistant_calls.append(args)
            raise AssertionError("abgewiesener Sprecher darf keine Tools starten")

    async def address_check(*args, **_kwargs):
        address_checks.append(args)
        return True

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    monkeypatch.setattr("core.speaker.get_speaker_verifier", FakeSpeakerVerifier)
    monkeypatch.setattr("config.VOICE_SPEAKER_VERIFY", True)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *args: logged.append(args))

    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )
    asyncio.run(endpoint(FakeWebSocket()))

    assert address_checks == []
    assert assistant_calls == []
    latency = next(event[2] for event in logged if event[0] == "voice_latency")
    assert latency["speaker_score"] == 0.21
    assert latency["speaker_ms"] == 7.5
    assert latency["speaker_decision"] == "rejected"


@pytest.mark.parametrize(
    ("score", "expected_control"),
    [(0.7, "stop_playback"), (0.2, "unduck_playback")],
    ids=["early-owner-score-stops", "low-score-waits-then-unducks"],
)
def test_bargein_uses_early_and_one_second_speaker_scores(
    monkeypatch, score, expected_control,
):
    import numpy as np
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    first_audio_sent = asyncio.Event()
    expected_control_sent = asyncio.Event()
    score_lengths = []

    class FakeSpeakerVerifier:
        async def has_profile(self):
            return True

        async def verify_pcm(self, _audio):
            return type(
                "Verification", (),
                {"score": 0.8, "elapsed_ms": 1.0, "decision": "accepted", "embedding": np.ones(192)},
            )()

        async def maybe_learn(self, *_args, **_kwargs):
            return False

        async def score_pcm(self, audio):
            score_lengths.append(len(audio))
            return type(
                "Score", (),
                {"score": score, "elapsed_ms": 2.0, "enrolled": True},
            )()

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {
                    "text": "Mantis, sag etwas.", "raw_text": "Mantis, sag etwas.",
                    "wakeword_triggered": True, "pcm_audio": b"\0\0" * 16_000,
                    "speech_pcm_audio": b"\0\0" * 16_000, "stt_ms": 1.0,
                }
            if chunk == b"bargein":
                await self.callbacks["on_bargein"](time.monotonic())
                self.callbacks["on_speech_progress"](b"\0\0" * 9_600, 600, 0)
                self.callbacks["on_speech_progress"](b"\0\0" * 16_000, 1_000, 0)
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_chunk":
                first_audio_sent.set()
            if data.get("type") == expected_control:
                expected_control_sent.set()

        async def receive(self):
            self.index += 1
            if self.index == 1:
                return {"text": json.dumps({"type": "hello", "audio": "stream"})}
            if self.index == 2:
                return {"bytes": b"initial"}
            if self.index == 3:
                await asyncio.wait_for(first_audio_sent.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.index == 4:
                await asyncio.wait_for(expected_control_sent.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, _text, **_kwargs):
            return "Antwort.", []

        def record_interrupted_voice_turn(self, _text):
            pass

    async def streaming_response(websocket, _text, _orch, *, playback, **_kwargs):
        block_id = playback.register_block(
            0, _fake_opus(1000), text="Antwort.", sentence_id=1, duration_ms=10_000,
        )
        playback.mark_block_sent(block_id)
        await websocket.send_json({"type": "audio_chunk", "block_id": block_id, "b64": ""})
        await asyncio.Future()

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "sende_streaming_antwort", streaming_response)
    monkeypatch.setattr("core.speaker.get_speaker_verifier", FakeSpeakerVerifier)
    monkeypatch.setattr("config.VOICE_SPEAKER_VERIFY", True)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)

    endpoint = next(
        route.endpoint for route in build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )
    asyncio.run(asyncio.wait_for(endpoint(FakeWebSocket()), timeout=2))

    assert score_lengths == [19_200, 32_000]


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


def test_voice_latency_event_includes_smart_turn_decision_fields(monkeypatch):
    logged = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            return "Hallo.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(), "Hi", FakeOrchestrator(),
        address_metrics={
            "turn_model_p": 0.91,
            "turn_model_ms": 13.4,
            "turn_decision": "model_complete",
            "silence_used_ms": 288,
        },
    ))

    detail = logged[0][2]
    assert detail["turn_model_p"] == 0.91
    assert detail["turn_model_ms"] == 13.4
    assert detail["turn_decision"] == "model_complete"
    assert detail["silence_used_ms"] == 288


def test_address_rejected_latency_does_not_look_like_backend_failure(monkeypatch):
    import web.routers.voice as voice_router
    from core import voice_timing

    logged = []

    class FallbackBackend:
        model_name = "unknown"
        last_used_backend = None

    class FakeOrchestrator:
        voice_agent = type("Agent", (), {"backend": FallbackBackend()})()

    monkeypatch.setattr(voice_router.db, "log_event", lambda *args: logged.append(args))
    token = voice_timing.start()
    try:
        timing = voice_timing.current()
        timing.llm_calls.append({"error": "CancelledError", "ms_total": 12.0})
        voice_router._record_voice_latency(
            FakeOrchestrator(), time.perf_counter(), stt_ms=1.0,
            address_rejected=True,
        )
    finally:
        voice_timing.reset(token)

    detail = logged[0][2]
    assert detail["address_rejected"] is True
    assert detail["stage"] == "address_rejected"
    assert detail["llm_calls"] == []


@pytest.mark.parametrize("text", [
    "stopp", "Halt, bitte", "stop", "warte kurz", "sei still",
    "Ich sage jetzt Stopp.",
])
def test_stop_reflex_is_recognized_even_in_partial_text(text):
    import web.routers.voice as voice_router

    assert voice_router._is_stop_reflex(text) is True


@pytest.mark.parametrize("text", ["Stoppuhr", "ich warte noch", "Stillstand"])
def test_stop_reflex_does_not_match_inside_unrelated_words(text):
    import web.routers.voice as voice_router

    assert voice_router._is_stop_reflex(text) is False


def test_stop_reflex_in_partial_text_stops_before_final_segment(monkeypatch):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    audio_sent = asyncio.Event()
    stop_sent = asyncio.Event()
    response_cancelled = asyncio.Event()
    second_response_finished = asyncio.Event()
    checks = []
    response_texts = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        async def handle_chunk(self, chunk, muted=False):
            if chunk == b"initial":
                return {"text": "Mantis, erzähl etwas.", "stt_ms": 1.0}
            if chunk == b"bargein":
                await self.callbacks["on_bargein"](time.monotonic())
                self.callbacks["on_partial"]("sei")
                self.callbacks["on_partial"]("sei still")
                await asyncio.sleep(0)
                return None
            if chunk == b"final":
                return {"text": "sei still", "stt_ms": 1.0}
            return None

        async def close(self):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.receive_index = 0

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data)
            if data.get("type") == "audio_chunk":
                audio_sent.set()
            elif data.get("type") == "stop_playback":
                stop_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"text": json.dumps({"type": "hello", "audio": "stream"})}
            if self.receive_index == 2:
                return {"bytes": b"initial"}
            if self.receive_index == 3:
                await asyncio.wait_for(audio_sent.wait(), timeout=1)
                return {"bytes": b"bargein"}
            if self.receive_index == 4:
                await asyncio.wait_for(stop_sent.wait(), timeout=1)
                return {"bytes": b"final"}
            if self.receive_index == 5:
                await asyncio.wait_for(second_response_finished.wait(), timeout=1)
                return {"type": "websocket.disconnect"}
            raise AssertionError("unerwartete zusätzliche WebSocket-Nachricht")

    class FakeOrchestrator:
        async def voice_respond(self, _text, **_kwargs):
            return "Okay.", []

    async def address_check(text, *, conversation_context=None):
        checks.append(text)
        return True

    async def streaming_response(websocket, text, _orch, *, playback, **_kwargs):
        response_texts.append(text)
        if len(response_texts) == 1:
            block_id = playback.register_block(
                0, _fake_opus(1000), text="Antwort.", sentence_id=1,
            )
            playback.mark_block_sent(block_id)
            await websocket.send_json({"type": "audio_chunk", "block_id": block_id})
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                response_cancelled.set()
                raise
        second_response_finished.set()
        return "Okay."

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    monkeypatch.setattr(voice_router, "sende_streaming_antwort", streaming_response)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(vad, "VadSegmenter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", False)
    websocket = FakeWebSocket()
    endpoint = next(
        route.endpoint for route in voice_router.build_router(FakeOrchestrator()).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=3))

    assert response_cancelled.is_set()
    assert "sei still" in response_texts
    assert checks == ["Mantis, erzähl etwas."]
    assert any(item.get("type") == "duck_playback" for item in websocket.sent)
    assert any(item.get("type") == "stop_playback" for item in websocket.sent)


def test_cached_filler_is_an_echo_with_its_playback_duration(monkeypatch):
    import web.routers.voice as voice_router
    from core.voice_fillers import VoiceFiller

    playback = voice_router._PlaybackTurn()
    filler_requested = asyncio.Event()

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            await asyncio.wait_for(filler_requested.wait(), timeout=0.5)
            return "Antwort.", []

    async def fake_tts(_sentence, **_kwargs):
        yield _fake_opus(700)

    filler = VoiceFiller("ack", "Moment.", _fake_opus(350), 350.0)
    def choose_filler(_kind):
        filler_requested.set()
        return filler

    monkeypatch.setattr(voice_router, "synthesize_stream", fake_tts)
    monkeypatch.setattr(voice_router.voice_fillers, "choose_cached_filler", choose_filler)
    monkeypatch.setattr(voice_router.tts, "_tts_turn_can_use_filler", lambda _state: True)
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 0)

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(), "Mantis, sag etwas", FakeOrchestrator(),
        playback=playback,
    ))

    filler_block = next(block for block in playback.blocks.values() if block.text == "Moment.")
    assert filler_block.duration_ms == 350.0
    echo_text = playback.echo_window_text(now=filler_block.sent_at + 0.2)
    assert "Moment." in echo_text
    assert voice_router._is_recent_tts_echo("Moment.", echo_text)
    assert "Moment." not in playback.echo_window_text(now=filler_block.sent_at + 2.36)


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
            from core import voice_timing
            voice_timing.mark_partial_reused(prework.elapsed_ms)
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
        partial_as_final=True,
    ))

    detail = logged[0][2]
    assert detail["partial_runs"] == 2
    assert detail["partial_as_final"] is True
    assert detail["partial_reused"] is True
    assert detail["saved_ms"] == 64.5


def test_live_websocket_router_reports_partial_stt_runs_and_skip_reasons(monkeypatch):
    import config
    import core.vad as vad
    import core.wakeword as wakeword
    import core.voice_stream as voice_stream
    import web.routers.voice as voice_router

    logged = []
    partial_calls = []
    response_texts = []

    class FakeVAD:
        def __init__(self):
            self.frames = 0

        def speech_probability(self, _frame):
            self.frames += 1
            return 0.9 if 14 <= self.frames < 64 else 0.0

    class FakeWakeword:
        def __init__(self):
            self.calls = 0

        def check(self, _frame):
            self.calls += 1
            return self.calls == 1

    async def partial_transcriber(_audio):
        partial_calls.append("partial")
        return "Mantis, erzähl kurz etwas", False

    async def final_transcriber(_audio):
        return "Mantis, erzähl kurz etwas", False

    async def fake_sende_antwort(*_args, **_kwargs):
        return 1.0

    async def forbidden_address_check(*_args, **_kwargs):
        raise AssertionError("Wakeword ist bereits die Freigabe")

    class FakeOrchestrator:
        async def voice_respond(self, text, **_kwargs):
            response_texts.append(text)
            return "Alles klar.", []

    async def run():
        latency_logged = asyncio.Event()

        class WebSocket:
            def __init__(self):
                silence = b"\0\0" * 128
                speech = b"\xe8\x03" * 128
                self.frames = iter(
                    [silence] * 52 + [speech] * 200 + [silence] * 60,
                )
                self.sent = []

            async def accept(self):
                pass

            async def send_json(self, data):
                self.sent.append(data)

            async def receive(self):
                try:
                    frame = next(self.frames)
                except StopIteration:
                    await asyncio.wait_for(latency_logged.wait(), timeout=3)
                    return {"type": "websocket.disconnect"}
                await asyncio.sleep(0.001)
                return {"bytes": frame}

        def log_event(*args):
            logged.append(args)
            if args[0] == "voice_latency":
                latency_logged.set()

        monkeypatch.setattr(vad, "SileroVAD", lambda _path: FakeVAD())
        monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: FakeWakeword())
        monkeypatch.setattr(voice_stream, "transcribe_pcm", final_transcriber)
        monkeypatch.setattr(voice_router, "transcribe_pcm_partial", partial_transcriber)
        monkeypatch.setattr(voice_router, "partial_stt_executor_available", lambda: True)
        monkeypatch.setattr(voice_stream.stt_runtime, "estimate_stt_runtime_ms", lambda _seconds: 100)
        monkeypatch.setattr(voice_router, "sende_antwort", fake_sende_antwort)
        monkeypatch.setattr(voice_router, "is_addressed_to_mantis", forbidden_address_check)
        monkeypatch.setattr(voice_router.db, "log_event", log_event)
        monkeypatch.setattr(config, "VOICE_PARTIAL_STT_MS", 32, raising=False)
        monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 900, raising=False)
        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", False, raising=False)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 300, raising=False)
        monkeypatch.setattr(config, "VOICE_BARGEIN", False, raising=False)
        websocket = WebSocket()
        endpoint = next(
            route.endpoint for route in voice_router.build_router(FakeOrchestrator()).routes
            if route.path == "/ws/voice/stream"
        )
        await asyncio.wait_for(endpoint(websocket), timeout=5)

    asyncio.run(run())
    detail = next(args[2] for args in logged if args[0] == "voice_latency")

    assert partial_calls
    assert response_texts == ["Mantis, erzähl kurz etwas"]
    assert detail["partial_runs"] >= 1
    assert detail["partial_skip_reasons"]["no_speech"] > 0
    assert set(detail["partial_skip_reasons"]) == {
        "busy", "budget", "interval", "no_speech", "disabled", "not_wired",
    }


def test_voice_latency_does_not_claim_reflex_prework_was_reused(monkeypatch):
    from core.voice_prework import VoiceTurnPrework

    logged = []
    prework = VoiceTurnPrework(
        text="Wie spät ist es",
        system="Vorbereiteter Kontext",
        keyword_tools=(),
        force_tools=False,
        elapsed_ms=82.0,
    )

    class ReflexOrchestrator:
        async def voice_respond(self, _text, stream_cb=None, prework=None):
            assert prework is not None
            from core import voice_timing
            voice_timing.mark_partial_reused(prework.elapsed_ms)
            return "Es ist 09:07 Uhr.", [{"tool": "voice_reflex", "reflex": True}]

    async def fake_tts(_sentence, **_kwargs):
        yield b"audio"

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))

    asyncio.run(sende_streaming_antwort(
        FakeWebSocket(),
        "Wie spät ist es?",
        ReflexOrchestrator(),
        prework=prework,
        partial_runs=1,
        partial_as_final=True,
    ))

    detail = logged[0][2]
    assert detail["reflex"] is True
    assert detail["partial_runs"] == 1
    assert detail["partial_as_final"] is True
    assert detail["partial_reused"] is False


def _pcm_frame(amplitude=0, sample_count=512):
    import struct

    return struct.pack(f"<{sample_count}h", *([amplitude] * sample_count))


def _make_real_voice_orchestrator(monkeypatch, responses):
    from types import SimpleNamespace

    import core.message_handler as message_handler
    from core.message_handler import MessageHandler
    from memory.kzg import KZG

    class Agent:
        backend = SimpleNamespace(sends_full_tool_catalog=True)
        model_name = "voice-test"

        def __init__(self):
            self.calls = []

        async def run(self, **kwargs):
            messages = kwargs["messages"]
            last = messages[-1]
            content = last.get("content") if isinstance(last, dict) else last.content
            text = next((item for item in responses if content.startswith(item)), content)
            keyword_tools = list(kwargs.get("allowed_tools", ()))
            keyword_force = bool(kwargs.get("force_tools", False))
            if "tool_selection_task" in kwargs:
                tools, force = await kwargs["tool_selection_task"]
            else:
                tools, force = keyword_tools, keyword_force
            self.calls.append({
                "text": text,
                "keyword_tools": keyword_tools,
                "keyword_force": keyword_force,
                "tools": list(tools),
                "force": bool(force),
                "prework": kwargs.get("prework"),
            })
            gate = kwargs.get("execution_gate")
            if gate is not None:
                assert await gate()
            reply, trace = responses[text]
            return reply, trace

    class PromptBuilder:
        async def build(self, _text, **_kwargs):
            return "Voice-Test-Kontext"

    agent = Agent()
    persisted = []
    kzg = KZG(max_turns=40)
    handler = MessageHandler(
        kzg=kzg,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args, **_kwargs: None),
        agent=agent,
        prompt_builder=PromptBuilder(),
        channel=None,
        proactive_tracker=None,
        forgetting=None,
        extractor=None,
        bg_llm=None,
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=lambda: None,
    )
    handler._persist_msg = lambda role, content, channel="telegram", meta=None, **_kwargs: persisted.append(
        (role, content, channel, meta or {}),
    )

    async def no_external_classification(_text):
        return None

    def discard_background_task(coro, _label):
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    monkeypatch.setattr(message_handler, "_safe_task", discard_background_task)
    monkeypatch.setattr(message_handler.fast_commands, "match", lambda _text: None)
    monkeypatch.setattr(message_handler.fast_commands, "match_voice", lambda _text: None)
    monkeypatch.setattr(message_handler.skill_request, "match", lambda _text: None)
    monkeypatch.setattr(message_handler.skills.T, "_semantic_rank", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        message_handler.skills.T.decisions, "tool_categories", no_external_classification,
    )

    class Orchestrator:
        voice_agent = agent
        prepared_texts = []

        async def prepare_voice_turn(self, text):
            self.prepared_texts.append(text)
            return await handler.prepare_voice_turn(text)

        async def voice_respond(self, text, prework=None, execution_gate=None):
            return await handler.dashboard_respond(
                text,
                agent=agent,
                channel_name="voice",
                prework=prework,
                execution_gate=execution_gate,
            )

    return Orchestrator(), handler, agent, persisted


def _install_real_voice_session_inputs(monkeypatch, partial_transcriber, final_transcriber):
    import core.vad as vad
    import core.voice_stream as voice_stream
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    class VAD:
        def speech_probability(self, frame):
            return 0.9 if any(frame) else 0.0

    class Wakeword:
        def __init__(self, *_args, **_kwargs):
            self.fired = False

        def check(self, frame):
            if any(frame) and not self.fired:
                self.fired = True
                return True
            return False

    monkeypatch.setattr(vad, "SileroVAD", lambda _path: VAD())
    monkeypatch.setattr(wakeword, "WakeWordDetector", Wakeword)
    monkeypatch.setattr(voice_stream, "transcribe_pcm", final_transcriber)
    monkeypatch.setattr(voice_router, "transcribe_pcm_partial", partial_transcriber)
    monkeypatch.setattr(voice_router, "partial_stt_executor_available", lambda: True)
    monkeypatch.setattr(
        voice_stream.stt_runtime, "estimate_stt_runtime_ms", lambda _seconds: 1,
    )
    monkeypatch.setattr(voice_router.db, "log_event", lambda *_args: None)


class _LiveVoiceWebSocket:
    def __init__(self, actions):
        self.actions = list(actions)
        self.sent = []
        self._response_count = 0
        self._changed = asyncio.Event()

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)
        if "audio_b64" in data:
            self._response_count += 1
            self._changed.set()

    async def _wait_for_responses(self, count):
        while self._response_count < count:
            self._changed.clear()
            if self._response_count >= count:
                return
            await self._changed.wait()

    async def receive(self):
        while self.actions:
            action = self.actions.pop(0)
            if isinstance(action, tuple) and action[0] == "wait":
                try:
                    await asyncio.wait_for(
                        self._wait_for_responses(action[1]), timeout=action[2],
                    )
                except TimeoutError:
                    pass
                continue
            if callable(action):
                action()
                continue
            if action is None:
                return {"type": "websocket.disconnect"}
            await asyncio.sleep(0.001)
            return {"bytes": action}
        return {"type": "websocket.disconnect"}


def test_incomplete_voice_segment_is_processed_without_continuation_timer(monkeypatch):
    import base64

    import config
    import web.routers.voice as voice_router

    text = "Mantis erinnere mich an"
    addressed_text = "Mantis, erinnere mich an"
    responses = {addressed_text: ("Worauf soll ich dich erinnern?", [])}
    orchestrator, handler, agent, persisted = _make_real_voice_orchestrator(
        monkeypatch, responses,
    )
    _install_real_voice_session_inputs(
        monkeypatch,
        partial_transcriber=lambda _audio: asyncio.sleep(0, result=(text, False)),
        final_transcriber=lambda _audio: asyncio.sleep(0, result=(text, False)),
    )
    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 2000, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_STT_MS", 1, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 900, raising=False)
    monkeypatch.setattr(config, "VOICE_FILLERS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "VOICE_BARGEIN", False, raising=False)

    async def synthesize(_text):
        return b"voice-test-audio"

    async def address_check(*_args, **_kwargs):
        raise AssertionError("Der Segmentstart wurde durch Wakeword freigegeben")

    monkeypatch.setattr(voice_router, "synthesize", synthesize)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)
    actions = (
        [_pcm_frame(0)] * 3
        + [_pcm_frame(1200)] * 20
        + [_pcm_frame(0)] * 70
        + [("wait", 1, 0.25), None]
    )
    websocket = _LiveVoiceWebSocket(actions)
    endpoint = next(
        route.endpoint for route in voice_router.build_router(orchestrator).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=5))

    audio_responses = [item for item in websocket.sent if "audio_b64" in item]
    assert len(audio_responses) == 1
    assert audio_responses[0]["text"] == addressed_text
    assert audio_responses[0]["audio_b64"] == base64.b64encode(b"voice-test-audio").decode()
    assert [call["text"] for call in agent.calls] == [addressed_text]
    assert [(turn.role, turn.content) for turn in handler.kzg.get_turns()] == [
        ("user", addressed_text), ("assistant", "Worauf soll ich dich erinnern?"),
    ]
    assert [(role, content) for role, content, _channel, _meta in persisted] == [
        ("user", addressed_text), ("assistant", "Worauf soll ich dich erinnern?"),
    ]


def test_incomplete_voice_phrase_spans_1800ms_pause_in_one_real_segment(monkeypatch):
    import base64

    import config
    import web.routers.voice as voice_router

    incomplete = "Mantis erinnere mich an"
    complete = "Mantis erinnere mich an den Müll morgen früh"
    addressed_complete = "Mantis, erinnere mich an den Müll morgen früh"
    responses = {addressed_complete: ("Ich erinnere dich morgen früh.", [{"tool": "set_reminder"}])}
    orchestrator, handler, agent, persisted = _make_real_voice_orchestrator(
        monkeypatch, responses,
    )
    final_segments = []

    async def partial_transcriber(audio):
        if len(audio) > 50_000:
            return None
        return incomplete, False

    async def final_transcriber(audio):
        final_segments.append(audio)
        return complete, False

    _install_real_voice_session_inputs(monkeypatch, partial_transcriber, final_transcriber)
    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 2000, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_STT_MS", 1, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
    monkeypatch.setattr(config, "VOICE_FILLERS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "VOICE_BARGEIN", False, raising=False)

    async def synthesize(_text):
        return b"voice-test-audio"

    async def address_check(*_args, **_kwargs):
        raise AssertionError("Das Segment wurde vom Wakeword freigegeben")

    monkeypatch.setattr(voice_router, "synthesize", synthesize)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)

    def assert_pause_has_not_ended_segment():
        assert final_segments == []
        assert websocket._response_count == 0

    # PCM in 8-ms worklet chunks: 225 silence chunks are genau 1,8 Sekunden.
    actions = (
        [_pcm_frame(0, 128)] * 3
        + [_pcm_frame(1200, 128)] * 80
        + [_pcm_frame(0, 128)] * 225
        + [assert_pause_has_not_ended_segment]
        + [_pcm_frame(1200, 128)] * 80
        + [_pcm_frame(0, 128)] * 275
        + [("wait", 1, 1.0), None]
    )
    websocket = _LiveVoiceWebSocket(actions)
    endpoint = next(
        route.endpoint for route in voice_router.build_router(orchestrator).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=8))

    audio_responses = [item for item in websocket.sent if "audio_b64" in item]
    assert len(final_segments) == 1
    assert len(agent.calls) == 1
    assert agent.calls[0]["text"] == addressed_complete
    assert "set_reminder" in agent.calls[0]["tools"]
    assert agent.calls[0]["force"] is True
    assert len(audio_responses) == 1
    assert audio_responses[0]["text"] == addressed_complete
    assert audio_responses[0]["audio_b64"] == base64.b64encode(b"voice-test-audio").decode()
    assert [(turn.role, turn.content) for turn in handler.kzg.get_turns()] == [
        ("user", addressed_complete), ("assistant", "Ich erinnere dich morgen früh."),
    ]
    assert [(role, content) for role, content, _channel, _meta in persisted] == [
        ("user", addressed_complete), ("assistant", "Ich erinnere dich morgen früh."),
    ]


def test_voice_router_limits_followup_tools_to_read_only_and_drops_old_action_context(
    monkeypatch,
):
    import base64

    import config
    import core.voice_stream as voice_stream
    import web.routers.voice as voice_router

    texts = (
        "Mantis, wie wird das Wetter morgen?",
        "Und nächste Woche?",
        "Wie wird das Wetter nächste Woche?",
        "Morgen früh?",
        "Lösche meine Aufgabe 42.",
        "Erzähl mir einen Witz.",
    )
    amplitudes = dict(zip((1200, 1800, 2400, 3000, 3600, 4200), texts, strict=True))
    responses = {
        texts[0]: ("Morgen wird es mild.", [{"tool": "get_weather"}]),
        texts[1]: ("Nächste Woche wird es kühler.", [{"tool": "get_weather"}]),
        texts[2]: ("Nächste Woche bleibt es trocken.", [{"tool": "get_weather"}]),
        texts[3]: ("Morgen früh bleibt es kühl.", [{"tool": "get_weather"}]),
        texts[4]: ("Die Aufgabe wurde gelöscht.", []),
        texts[5]: ("Warum ging der Computer zum Arzt? Wegen eines Virus.", []),
    }
    orchestrator, handler, agent, persisted = _make_real_voice_orchestrator(
        monkeypatch, responses,
    )

    def text_for_audio(audio):
        samples = memoryview(audio).cast("h")
        marker = max((abs(sample) for sample in samples), default=0)
        return amplitudes[marker]

    async def partial_transcriber(audio):
        return text_for_audio(audio), False

    async def final_transcriber(audio):
        return text_for_audio(audio), False

    _install_real_voice_session_inputs(monkeypatch, partial_transcriber, final_transcriber)
    monkeypatch.setattr(voice_stream.stt_runtime, "estimate_stt_runtime_ms", lambda _seconds: 1)
    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", False, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_MS", 64, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_STT_MS", 1, raising=False)
    monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 1200, raising=False)
    monkeypatch.setattr(config, "VOICE_FILLERS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "VOICE_BARGEIN", False, raising=False)

    async def synthesize(_text):
        return b"voice-test-audio"

    address_checks = []

    async def address_check(text, **_kwargs):
        address_checks.append(text)
        return True

    monkeypatch.setattr(voice_router, "synthesize", synthesize)
    monkeypatch.setattr(voice_router, "is_addressed_to_mantis", address_check)

    actions = [_pcm_frame(0)] * 3
    for index, amplitude in enumerate(amplitudes):
        actions.extend([_pcm_frame(amplitude)] * 20)
        actions.extend([_pcm_frame(0)] * 4)
        actions.append(("wait", index + 1, 1.0))
    actions.append(None)
    websocket = _LiveVoiceWebSocket(actions)
    endpoint = next(
        route.endpoint for route in voice_router.build_router(orchestrator).routes
        if route.path == "/ws/voice/stream"
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=10))

    assert len(agent.calls) == 6
    weather_followup = agent.calls[1]
    assert "get_weather" in weather_followup["keyword_tools"]
    assert "get_weather" in weather_followup["tools"]
    assert weather_followup["keyword_force"] is False
    assert weather_followup["force"] is False
    assert set(weather_followup["tools"]) <= {
        "get_weather", "web_search", "brain_search",
    }

    time_followup = agent.calls[3]
    assert "get_weather" in time_followup["keyword_tools"]
    assert "get_weather" in time_followup["tools"]
    assert time_followup["keyword_force"] is False
    assert time_followup["force"] is False
    assert set(time_followup["tools"]) <= {
        "get_weather", "web_search", "brain_search",
    }

    delete_turn = agent.calls[4]
    assert delete_turn["keyword_force"] is True
    assert delete_turn["force"] is True
    assert "set_reminder" in delete_turn["tools"]

    joke_turn = agent.calls[5]
    # Kein geerbter Aktionskontext: kein Zwang, keine Erinnerungs-Freigabe aus dem Löschbefehl.
    # Die Standardauswahl ohne Vorturn bleibt erhalten (wie auf main).
    assert "set_reminder" not in joke_turn["keyword_tools"]
    assert joke_turn["keyword_force"] is False
    assert joke_turn["force"] is False
    assert "set_reminder" not in joke_turn["tools"]
    assert set(texts[1:]) <= set(orchestrator.prepared_texts)

    audio_responses = [item for item in websocket.sent if "audio_b64" in item]
    assert len(audio_responses) == 6
    assert all(
        item["audio_b64"] == base64.b64encode(b"voice-test-audio").decode()
        for item in audio_responses
    )
    assert [item["text"] for item in audio_responses] == list(texts)
    assert [(turn.role, turn.content) for turn in handler.kzg.get_turns()] == [
        item
        for text, (reply, _trace) in responses.items()
        for item in (("user", text), ("assistant", reply))
    ]
    assert [(role, content) for role, content, _channel, _meta in persisted] == [
        item
        for text, (reply, _trace) in responses.items()
        for item in (("user", text), ("assistant", reply))
    ]
    assert address_checks == list(texts[1:])


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


def test_websocket_disconnect_closes_voice_session_and_partial_cache(monkeypatch):
    import core.vad as vad
    import core.wakeword as wakeword
    import web.routers.voice as voice_router

    sessions = []
    caches = []

    class FakeSession:
        def __init__(self, **_kwargs):
            self.closed = False
            sessions.append(self)

        async def close(self):
            self.closed = True

        async def handle_chunk(self, *_args, **_kwargs):
            return None

    class FakeCache:
        def __init__(self, _prepare):
            self.closed = False
            caches.append(self)

        def offer(self, _text):
            pass

        def take_for(self, _text):
            return None

        def clear(self):
            pass

        async def close(self):
            self.closed = True

    class FakeWebSocket:
        async def accept(self):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect"}

    class FakeOrchestrator:
        async def prepare_voice_turn(self, _text):
            return None

    monkeypatch.setattr(voice_router, "VoiceStreamSession", FakeSession)
    monkeypatch.setattr(voice_router, "VoicePreworkCache", FakeCache)
    monkeypatch.setattr(vad, "SileroVAD", lambda _path: object())
    monkeypatch.setattr(wakeword, "WakeWordDetector", lambda _path: object())

    router = build_router(FakeOrchestrator())
    endpoint = next(route.endpoint for route in router.routes if route.path == "/ws/voice/stream")
    asyncio.run(endpoint(FakeWebSocket()))

    assert len(sessions) == 1 and sessions[0].closed
    assert len(caches) == 1 and caches[0].closed


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
    from core import tts

    clock = FakeClock()
    logged = []
    chosen = []

    class FakeOrchestrator:
        async def voice_respond(self, _text, stream_cb=None):
            tts._active_tts_turn.get().engine = "google"
            await stream_cb(attempt_start("slow-stt-turn"))
            await stream_cb(delta("slow-stt-turn", "Hier bin ich."))
            await stream_cb(attempt_end("slow-stt-turn", final=True))
            return "Hier bin ich.", []

    async def fake_tts(_sentence, **_kwargs):
        yield b"answer-audio"

    def choose(kind):
        chosen.append(kind)
        raise AssertionError("Fallback-Stimme darf keinen Hauptstimmen-Füller auswählen")

    monkeypatch.setattr("web.routers.voice.synthesize_stream", fake_tts)
    monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
    monkeypatch.setattr("core.voice_fillers.choose_cached_filler", choose)
    monkeypatch.setattr("config.VOICE_FILLERS_ENABLED", True)
    monkeypatch.setattr("config.VOICE_FILLER_ACK_DELAY_MS", 800)
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN", ["cartesia", "google", "local"])
    monkeypatch.setattr("config.TTS_ENGINE_CHAIN_EXPLICIT", True)
    ws = FakeWebSocket()

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

    assert chosen == []
    assert logged[0][2]["filler"] is None


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
