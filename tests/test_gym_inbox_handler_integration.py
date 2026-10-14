"""Gemeinsame Regressionstests für Gym- und Merk-Inbox-Fast-Paths."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from communication.base import IncomingMessage
from core import db
from core.message_handler import MessageHandler
from domains.gym import coach as coach_module
from domains.gym import session as gym_session
from domains.gym.aliases import builtin_aliases


def _handler(monkeypatch):
    rows = []

    class KZG:
        def __init__(self):
            self.added = []

        def add(self, role, text):
            self.added.append((role, text))

        def get_recent_turns(self, n):
            return []

        def recent_messages(self, **_kwargs):
            return []

    class LZG:
        def __init__(self):
            self.saved = []

        def save_kzg_turn(self, role, text):
            self.saved.append((role, text))

    channel = SimpleNamespace(
        send=AsyncMock(return_value="reply-1"),
        send_typing=AsyncMock(),
        stop_typing=AsyncMock(),
        supports_streaming=False,
    )
    agent = SimpleNamespace(
        model_name="test-chain",
        _backend=SimpleNamespace(supports_vision=True),
        run=AsyncMock(return_value=("Normale Agentenantwort", [])),
    )
    kzg, lzg = KZG(), LZG()
    handler = MessageHandler(
        kzg=kzg,
        lzg=lzg,
        agent=agent,
        prompt_builder=SimpleNamespace(
            build=AsyncMock(return_value="normal system"),
            _last_mem_ids=[],
            embed_llm=object(),
        ),
        channel=channel,
        proactive_tracker=SimpleNamespace(record_user_response=Mock()),
        forgetting=SimpleNamespace(bump_recall=Mock()),
        extractor=SimpleNamespace(extract_from_exchange=AsyncMock(return_value=0)),
        bg_llm=SimpleNamespace(chat=AsyncMock()),
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=Mock(),
        channels={"discord": channel},
    )
    handler.gym_coach._latest_pending = lambda: None
    monkeypatch.setattr(coach_module, "load_aliases", builtin_aliases)
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=([], False)),
    )
    monkeypatch.setattr("core.message_handler.ui_state.maybe_update_ui", lambda *_args: None)
    monkeypatch.setattr(
        "core.message_handler._safe_task",
        lambda coro, _label: coro.close() if hasattr(coro, "close") else None,
    )
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gym_session, "open_session", lambda *_args, **_kwargs: None)

    def execute(sql, params=()):
        if "INSERT INTO chat_messages" in sql:
            rows.append(params)

    monkeypatch.setattr(db, "execute", execute)
    return handler, rows, kzg, lzg, agent, channel


def _discord_message(text, *, message_id, sender_id="123", reply_to_message_id=None):
    return IncomingMessage(
        text,
        sender_id,
        datetime(2026, 10, 14),
        raw=SimpleNamespace(channel=SimpleNamespace(id="discord-dm-123")),
        channel="discord",
        source_message_id=message_id,
        reply_to_message_id=reply_to_message_id,
    )


def _persisted(rows, role):
    return [row for row in rows if row[0] == role]


def _meta(row):
    return json.loads(row[3])


def _add_sets(parsed, **_kwargs):
    return {
        "exercise": parsed.exercise,
        "sets": [
            {"weight_kg": 80, "reps": 8},
            {"weight_kg": 80, "reps": 8},
            {"weight_kg": 80, "reps": 7},
        ],
        "previous_sets": [],
        "progression": {"weight_kg": 80, "target_reps": [8, 8, 8]},
        "records": {},
    }


def test_gym_message_is_routed_before_inbox_capture_and_persisted_once(monkeypatch):
    handler, rows, kzg, lzg, agent, channel = _handler(monkeypatch)
    monkeypatch.setattr(gym_session, "add_sets", _add_sets)
    captured = AsyncMock()
    monkeypatch.setattr("core.message_handler.inbox_service.capture_discord_message", captured)
    message = _discord_message("Bank 80 8 8 7", message_id="gym-1")

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    user_rows = _persisted(rows, "user")
    assert len(user_rows) == 1
    assert user_rows[0][1] == message.text
    assert _meta(user_rows[0])["use_case"] == "gym"
    assert _meta(user_rows[0])["action"]
    assert len(_persisted(rows, "assistant")) == 1
    assert not kzg.added
    assert not lzg.saved
    assert agent.run.await_count == 0
    captured.assert_not_awaited()
    assert channel.send.await_args.args[0].startswith("✅ Bankdrücken")


def test_bare_youtube_link_is_captured_by_inbox_with_one_user_turn(monkeypatch):
    handler, rows, kzg, lzg, agent, _channel = _handler(monkeypatch)
    captured = []

    async def capture(msg, _route, **_kwargs):
        captured.append(msg.text)
        return {"response": "📥 Gemerkt."}

    monkeypatch.setattr("core.message_handler.inbox_service.capture_discord_message", capture)
    message = _discord_message("https://youtu.be/abc", message_id="inbox-1")

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    user_rows = _persisted(rows, "user")
    assert captured == [message.text]
    assert len(user_rows) == 1
    assert _meta(user_rows[0]) == {"use_case": "inbox"}
    assert len(_persisted(rows, "assistant")) == 1
    assert kzg.added == [("user", message.text), ("assistant", "📥 Gemerkt.")]
    assert lzg.saved == kzg.added
    assert agent.run.await_count == 0


def test_delete_command_then_ja_deletes_inbox_item_once(monkeypatch):
    handler, rows, kzg, lzg, agent, _channel = _handler(monkeypatch)
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.resolve_item",
        lambda **_kwargs: {"status": "found", "item": {"id": 5, "title": "Video"}},
    )
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kwargs: deleted.append(kwargs) or {
            "status": "deleted", "item": {"id": 5, "title": "Video"},
        },
    )

    asyncio.run(handler.handle(_discord_message("lösch #5", message_id="delete-1"), None))
    asyncio.run(handler.handle(_discord_message("ja", message_id="confirm-1"), None))

    user_rows = _persisted(rows, "user")
    assert deleted == [{"item_id": 5}]
    assert [row[1] for row in user_rows] == ["lösch #5", "ja"]
    assert [_meta(row) for row in user_rows] == [
        {"use_case": "inbox"}, {"use_case": "inbox"},
    ]
    assert len(user_rows) == 2
    assert len(_persisted(rows, "assistant")) == 2
    assert kzg.added.count(("user", "lösch #5")) == 1
    assert kzg.added.count(("user", "ja")) == 1
    assert len(lzg.saved) == 4
    assert agent.run.await_count == 0


def test_gym_turn_expires_pending_inbox_delete_confirmation(monkeypatch):
    handler, rows, kzg, lzg, agent, _channel = _handler(monkeypatch)
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.resolve_item",
        lambda **_kwargs: {"status": "found", "item": {"id": 5, "title": "Video"}},
    )
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kwargs: deleted.append(kwargs) or {"status": "deleted"},
    )
    monkeypatch.setattr(gym_session, "add_sets", _add_sets)

    asyncio.run(handler.handle(_discord_message("lösch #5", message_id="delete-2"), None))
    asyncio.run(handler.handle(_discord_message("Bank 80 8 8 7", message_id="gym-2"), None))
    asyncio.run(handler.handle(_discord_message("ja", message_id="confirm-2"), None))

    user_rows = _persisted(rows, "user")
    assert deleted == []
    assert len(user_rows) == 3
    assert [row[1] for row in user_rows] == ["lösch #5", "Bank 80 8 8 7", "ja"]
    assert [_meta(row).get("use_case") for row in user_rows] == ["inbox", "gym", None]
    assert kzg.added.count(("user", "Bank 80 8 8 7")) == 0
    assert lzg.saved.count(("user", "Bank 80 8 8 7")) == 0
    assert agent.run.await_count == 1
