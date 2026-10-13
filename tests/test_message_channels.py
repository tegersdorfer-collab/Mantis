import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from communication.base import IncomingMessage
from core import db, log_observability
from core.idle_loop import IdleLoop
from core.message_handler import MessageHandler
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orchestrator import Orchestrator
from web.routers import chat, voice


def _make_orchestrator(monkeypatch):
    inserted = []

    class KZG:
        def add(self, *_args):
            return None

        def get_recent_turns(self, n):
            return []

        def recent_messages(self, max_tokens):
            return []

    idle_loop = object.__new__(IdleLoop)
    idle_loop._state = "idle"
    idle_loop._task = None

    async def wait_until_cancelled():
        await asyncio.Event().wait()

    idle_loop._loop = wait_until_cancelled
    channel = SimpleNamespace(
        supports_streaming=False,
        send=AsyncMock(),
        send_typing=AsyncMock(),
    )
    agent = SimpleNamespace(
        model_name="channel-test-model",
        run=AsyncMock(return_value=("Antwort aus dem Agenten", [])),
    )
    handler = MessageHandler(
        kzg=KZG(),
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        agent=agent,
        prompt_builder=SimpleNamespace(
            build=AsyncMock(return_value="system prompt"),
            _last_mem_ids=[],
        ),
        channel=channel,
        proactive_tracker=SimpleNamespace(record_user_response=lambda: None),
        forgetting=SimpleNamespace(bump_recall=lambda *_args: None),
        extractor=SimpleNamespace(extract_from_exchange=AsyncMock(return_value=0)),
        bg_llm=None,
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=idle_loop.pause,
    )

    def execute(sql, params=()):
        if "INSERT INTO chat_messages" in sql:
            inserted.append((sql, params))

    def discard_background(coro, _label):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", AsyncMock(return_value=([], [])))
    monkeypatch.setattr("core.message_handler.ui_state.maybe_update_ui", lambda *_args: None)
    monkeypatch.setattr("core.message_handler._safe_task", discard_background)
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)

    orchestrator = object.__new__(Orchestrator)
    orchestrator.msg_handler = handler
    orchestrator.voice_agent = agent
    orchestrator.idle_loop = idle_loop
    return orchestrator, inserted, idle_loop


def _persisted_role_channels(inserted):
    return [(params[0], params[2]) for _sql, params in inserted]


def test_incoming_message_defaults_to_telegram_channel():
    message = IncomingMessage("hi", "1", datetime(2026, 10, 13))

    assert message.channel == "telegram"


def test_telegram_handler_persists_both_roles_with_telegram_channel(monkeypatch):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    message = IncomingMessage("hello", "1", datetime(2026, 10, 13), channel="telegram")

    asyncio.run(orchestrator.handle_message(message))

    assert _persisted_role_channels(inserted) == [
        ("user", "telegram"),
        ("assistant", "telegram"),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


@pytest.mark.parametrize("channel_name", ["web", "ios", "mcp"])
def test_dashboard_routes_persist_both_roles_with_ingress_channel(monkeypatch, channel_name):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    app = FastAPI()
    app.include_router(chat.build_router(orchestrator))

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"text": "hello", "channel": channel_name},
        )

    assert response.status_code == 200
    assert response.json()["response"] == "Antwort aus dem Agenten"
    assert _persisted_role_channels(inserted) == [
        ("user", channel_name),
        ("assistant", channel_name),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_voice_route_persists_both_roles_with_voice_channel(monkeypatch):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    monkeypatch.setattr(voice, "transcribe_audio", AsyncMock(return_value="Wie wird mein Tag?"))
    monkeypatch.setattr(voice, "is_addressed_to_mantis", AsyncMock(return_value=True))
    monkeypatch.setattr(voice, "mark_conversation_active", lambda: None)
    monkeypatch.setattr(voice, "synthesize", AsyncMock(return_value=b"audio"))
    app = FastAPI()
    app.include_router(voice.build_router(orchestrator))

    with TestClient(app) as client:
        response = client.post(
            "/api/voice/segment",
            files={"audio": ("segment.wav", b"audio input", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "Antwort aus dem Agenten"
    assert _persisted_role_channels(inserted) == [
        ("user", "voice"),
        ("assistant", "voice"),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


@pytest.mark.parametrize("facade", ["dashboard", "voice", "telegram"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_turn_failure_or_cancellation_always_resumes_idle_loop(monkeypatch, facade, failure):
    orchestrator, _inserted, idle_loop = _make_orchestrator(monkeypatch)

    async def fail_turn(*_args, **_kwargs):
        raise failure("turn aborted")

    orchestrator.msg_handler._agent_turn = fail_turn

    async def run_turn():
        if facade == "dashboard":
            await orchestrator.dashboard_respond("hello")
        elif facade == "voice":
            await orchestrator.voice_respond("hello")
        else:
            message = IncomingMessage("hello", "1", datetime(2026, 10, 13))
            await orchestrator.handle_message(message)

    with pytest.raises(failure, match="turn aborted"):
        asyncio.run(run_turn())

    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_user_active_window_still_gates_background_work_for_30_minutes(monkeypatch):
    orchestrator = object.__new__(Orchestrator)
    now = [100.0]
    monkeypatch.setattr("orchestrator.time.time", lambda: now[0])
    orchestrator._last_user_ts = now[0] - 1800
    orchestrator.idle_loop = object.__new__(IdleLoop)
    orchestrator.idle_loop._state = "idle"
    orchestrator.idle_loop._task = None

    assert not orchestrator._user_active()
    orchestrator._mark_user_active()
    assert orchestrator.idle_loop._state == "conversation"
    now[0] += 1799
    assert orchestrator._user_active()
    now[0] += 1
    assert not orchestrator._user_active()


def test_persistence_failures_are_logged_to_error_observability(monkeypatch, caplog):
    events = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(db, "execute", fail)
    monkeypatch.setattr(db, "log_event", lambda *args: events.append(args))
    handler = object.__new__(MessageHandler)
    handler.lzg = SimpleNamespace(save_kzg_turn=fail)
    db_handler = log_observability.DBLogHandler()
    logger = logging.getLogger("core.message_handler")
    logger.addHandler(db_handler)
    try:
        with caplog.at_level(logging.ERROR, logger="core.message_handler"):
            handler._persist_msg("user", "hi", channel="web")
    finally:
        logger.removeHandler(db_handler)

    assert sum(record.levelno == logging.ERROR for record in caplog.records) == 2
    assert len(events) == 2
