"""Fast-Path- und Messbarkeitsvertrag am gemeinsamen Nachrichtenpfad."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from communication.base import IncomingMessage
from core import db
from core.message_handler import MessageHandler


def _handler(monkeypatch):
    rows = []

    class KZG:
        def __init__(self):
            self.turns = []

        def add(self, role, content):
            self.turns.append((role, content))

        def get_recent_turns(self, n):
            return []

        def recent_messages(self, max_tokens):
            return []

    class LZG:
        def save_kzg_turn(self, role, content):
            return None

    channel = SimpleNamespace(
        send=AsyncMock(), send_typing=AsyncMock(), stop_typing=AsyncMock(),
        supports_streaming=False,
    )
    backend = SimpleNamespace(supports_vision=True)
    agent = SimpleNamespace(
        model_name="openai/gpt-6-luna", _backend=backend,
        run=AsyncMock(return_value=("Antwort aus dem Agenten", [])),
    )
    handler = MessageHandler(
        kzg=KZG(), lzg=LZG(), agent=agent,
        prompt_builder=SimpleNamespace(
            build=AsyncMock(return_value="prompt"), _last_mem_ids=[], embed_llm=object(),
        ),
        channel=channel,
        proactive_tracker=SimpleNamespace(record_user_response=lambda: None),
        forgetting=SimpleNamespace(bump_recall=lambda *_a: None),
        extractor=SimpleNamespace(extract_from_exchange=AsyncMock(return_value=0)),
        bg_llm=None,
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=lambda: None,
        channels={"discord": channel},
    )

    def execute(sql, params=()):
        if "INSERT INTO chat_messages" in sql:
            rows.append((sql, params))
        return 1

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "query", lambda *_a, **_k: [])
    monkeypatch.setattr("core.message_handler.ui_state.maybe_update_ui", lambda *_a: None)
    monkeypatch.setattr("core.message_handler._safe_task", lambda coro, _label: _close(coro))
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)
    return handler, rows, agent, channel


def _close(coro):
    if hasattr(coro, "close"):
        coro.close()


def _meta_by_role(rows):
    return {params[0]: json.loads(params[3]) for _sql, params in rows}


def test_discord_capture_uses_fast_path_and_tags_both_chat_rows(monkeypatch):
    handler, rows, agent, channel = _handler(monkeypatch)
    captured = []

    async def capture(msg, route, **kwargs):
        captured.append((msg, route, kwargs))
        return {"status": "created", "response": "📥 gemerkt: „YouTube-Link“ · Anschauen · ID 41"}

    monkeypatch.setattr("core.message_handler.inbox_service.capture_discord_message", capture)
    message = IncomingMessage(
        "https://youtu.be/abc", "123", datetime(2026, 10, 14),
        channel="discord", source_message_id="discord-message-1",
    )

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    assert len(captured) == 1
    assert captured[0][2]["vision_backend"] is agent._backend
    assert agent.run.await_count == 0
    meta = _meta_by_role(rows)
    assert meta["user"] == {"use_case": "inbox"}
    assert meta["assistant"] == {
        "use_case": "inbox", "tools": [], "model": "openai/gpt-6-luna",
    }
    channel.send.assert_not_awaited()  # capture service owns the direct confirmation


def test_inbox_search_uses_agent_tools_but_skips_memory_extraction(monkeypatch):
    handler, rows, agent, _channel = _handler(monkeypatch)
    selected = []

    async def select(text):
        selected.append(text)
        return ["inbox_search"], True

    monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select)
    message = IncomingMessage(
        "Welche Filme habe ich mir gemerkt?", "123", datetime(2026, 10, 14),
        channel="discord", source_message_id="discord-message-2",
    )

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    assert agent.run.await_count == 1
    assert agent.run.await_args.kwargs["allowed_tools"] == ["inbox_search"]
    assert agent.run.await_args.kwargs["force_tools"] is True
    assert handler.extractor.extract_from_exchange.await_count == 0
    assert _meta_by_role(rows)["user"] == {"use_case": "inbox"}
    assert _meta_by_role(rows)["assistant"]["use_case"] == "inbox"


def test_link_question_remains_normal_agent_chat_without_inbox_metadata(monkeypatch):
    handler, rows, agent, _channel = _handler(monkeypatch)
    monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", AsyncMock(return_value=([], False)))
    message = IncomingMessage(
        "Was ist das? https://youtu.be/abc", "123", datetime(2026, 10, 14),
        channel="discord", source_message_id="discord-message-3",
    )

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    assert agent.run.await_count == 1
    assert "use_case" not in _meta_by_role(rows)["user"]
    assert "use_case" not in _meta_by_role(rows)["assistant"]
