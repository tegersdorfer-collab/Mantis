"""Gemeinsame Regressionstests für Gym- und Merk-Inbox-Fast-Paths."""

import asyncio
import json
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from communication.base import IncomingMessage
from core import db
from core.message_handler import MessageHandler, PendingInboxDeletion
from domains.agent_remote import AgentRemoteConfig, AgentRemoteService
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

        def save_kzg_turn(self, role, text, meta=None):
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
        raw=SimpleNamespace(
            author=SimpleNamespace(id=int(sender_id), bot=False),
            channel=SimpleNamespace(id="discord-dm-123"),
            guild=None,
        ),
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


def _git_repo(path):
    path.mkdir()
    commands = [
        ["git", "init", "--initial-branch=main"],
        ["git", "config", "user.name", "Integration Test"],
        ["git", "config", "user.email", "integration@example.invalid"],
    ]
    for command in commands:
        subprocess.run(command, cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("integration repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path,
        check=True, capture_output=True, text=True,
    )
    return path


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


@pytest.mark.parametrize(
    ("open_interceptor", "text", "expected_path"),
    [
        ("clarification", "https://youtu.be/abc", "capture"),
        ("clarification", "Welche Filme habe ich mir gemerkt?", "search"),
        ("daily_anchor", "https://youtu.be/abc", "capture"),
        ("daily_anchor", "Welche Filme habe ich mir gemerkt?", "search"),
    ],
)
def test_inbox_turn_bypasses_open_clarification_and_daily_anchor(
    monkeypatch, open_interceptor, text, expected_path,
):
    from domains import accountability as accountability_module

    real_intercept = accountability_module.intercept
    handler, rows, _kzg, _lzg, agent, _channel = _handler(monkeypatch)
    captured = AsyncMock(return_value={"response": "📥 Gemerkt."})
    monkeypatch.setattr("core.message_handler.inbox_service.capture_discord_message", captured)
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=(["inbox_search"], True)),
    )

    executed_sql = []
    execute = db.execute

    def record_execute(sql, params=()):
        executed_sql.append(sql)
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", record_execute)

    anchor_reads = []
    anchor_writes = []
    if open_interceptor == "clarification":
        monkeypatch.setattr(
            db,
            "query",
            lambda sql, *_args, **_kwargs: (
                [{"id": 91}] if "waiting_clarification" in sql else []
            ),
        )
    else:
        monkeypatch.setattr(accountability_module.config, "ACCOUNTABILITY_ENABLED", True)
        monkeypatch.setattr(
            accountability_module.config, "AUTONOMOUS_MESSAGES_ENABLED", True,
        )

        def open_daily_anchor(_day):
            anchor_reads.append(True)
            return {"anchor_sent_at": datetime(2026, 10, 14), "priority": None}

        def record_anchor_update(_day, **fields):
            anchor_writes.append(fields)

        def intercept(text):
            return real_intercept(text)

        monkeypatch.setattr(accountability_module, "_row", open_daily_anchor)
        monkeypatch.setattr(accountability_module, "_set", record_anchor_update)
        monkeypatch.setattr(accountability_module, "intercept", intercept)

    message = _discord_message(text, message_id=f"{open_interceptor}-{expected_path}")
    asyncio.run(handler.handle(message, resume_idle_cb=None))

    user_rows = _persisted(rows, "user")
    assert len(user_rows) == 1
    assert user_rows[0][1] == text
    assert _meta(user_rows[0]) == {"use_case": "inbox"}
    if expected_path == "capture":
        captured.assert_awaited_once()
        assert captured.await_args.args[0].text == text
        assert agent.run.await_count == 0
    else:
        captured.assert_not_awaited()
        assert agent.run.await_count == 1
        assert agent.run.await_args.kwargs["allowed_tools"] == ["inbox_search"]
        assert agent.run.await_args.kwargs["force_tools"] is True

    if open_interceptor == "clarification":
        assert not any("UPDATE tasks SET clarification_answer" in sql for sql in executed_sql)
    else:
        assert anchor_reads == []
        assert anchor_writes == []


def test_agent_gym_and_inbox_turns_share_order_and_expire_remote_confirmation(
    monkeypatch, tmp_path,
):
    from orchestrator import Orchestrator

    handler, rows, kzg, lzg, agent, channel = _handler(monkeypatch)
    monkeypatch.setattr(gym_session, "add_sets", _add_sets)
    captured = []

    async def capture(msg, _route, **_kwargs):
        captured.append(msg.text)
        return {"response": "📥 Gemerkt."}

    monkeypatch.setattr("core.message_handler.inbox_service.capture_discord_message", capture)
    repo = _git_repo(tmp_path / "repo")
    started = []

    def disabled_launcher(*args, **_kwargs):
        started.append(args)
        raise OSError("test launcher must not start")

    remote = AgentRemoteService(
        AgentRemoteConfig(
            enabled=True,
            allowed_user_ids=("123",),
            repos={"mantis": str(repo)},
            state_dir=tmp_path / "remote-state",
            home_dir=tmp_path / "home",
        ),
        persist=handler._persist_msg,
        popen=disabled_launcher,
    )
    handler.agent_remote = remote
    orchestrator = object.__new__(Orchestrator)
    orchestrator.agent_remote = remote
    orchestrator.msg_handler = handler
    orchestrator.idle_loop = SimpleNamespace(resume=Mock())

    async def run():
        await orchestrator.handle_message(
            _discord_message("codex: mantis analysiere README", message_id="agent-1"),
        )
        assert len(channel.send.await_args_list) == 1
        assert channel.send.await_args.args[0].startswith("Vorschlag-ID:")
        proposal_id = remote.pending["123"].proposal_id[:4]
        assert handler._incoming_message_sequence == 1

        await orchestrator.handle_message(
            _discord_message("Bank 80 8 8 7", message_id="gym-remote-expiry"),
        )
        assert remote.pending == {}
        assert handler._incoming_message_sequence == 2

        await orchestrator.handle_message(
            _discord_message("https://youtu.be/abc", message_id="inbox-after-gym"),
        )
        assert captured == ["https://youtu.be/abc"]
        assert handler._incoming_message_sequence == 3

        await orchestrator.handle_message(
            _discord_message(f"ja {proposal_id}", message_id="stale-confirmation"),
        )
        assert handler._incoming_message_sequence == 4
        await remote.stop()

    asyncio.run(run())

    assert started == []
    for text, use_case in (
        ("codex: mantis analysiere README", "agent"),
        ("Bank 80 8 8 7", "gym"),
        ("https://youtu.be/abc", "inbox"),
    ):
        user_rows = [row for row in _persisted(rows, "user") if row[1] == text]
        assert len(user_rows) == 1
        assert _meta(user_rows[0])["use_case"] == use_case

    preview_rows = [
        row for row in _persisted(rows, "assistant")
        if row[1].startswith("Vorschlag-ID:")
    ]
    assert len(preview_rows) == 1
    assert _meta(preview_rows[0]) == {"use_case": "agent", "untrusted": True}
    assert len([
        row for row in _persisted(rows, "assistant")
        if _meta(row).get("use_case") == "gym"
    ]) == 1
    assert len([
        row for row in _persisted(rows, "assistant")
        if _meta(row).get("use_case") == "inbox"
    ]) == 1
    assert ("user", "Bank 80 8 8 7") not in kzg.added
    assert ("user", "codex: mantis analysiere README") not in kzg.added
    assert ("assistant", preview_rows[0][1]) not in kzg.added
    assert ("user", "https://youtu.be/abc") in kzg.added
    assert "codex: mantis analysiere README" not in [text for _role, text in lzg.saved]
    assert not any(text.startswith("Vorschlag-ID:") for _role, text in lzg.saved)
    assert agent.run.await_count == 1


def test_inbox_delete_confirmation_wins_and_expires_remote_preview(monkeypatch, tmp_path):
    from orchestrator import Orchestrator

    handler, rows, _kzg, _lzg, _agent, _channel = _handler(monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    remote = AgentRemoteService(
        AgentRemoteConfig(
            enabled=True,
            allowed_user_ids=("123",),
            repos={"mantis": str(repo)},
            state_dir=tmp_path / "remote-state",
            home_dir=tmp_path / "home",
        ),
        persist=handler._persist_msg,
    )
    handler.agent_remote = remote
    orchestrator = object.__new__(Orchestrator)
    orchestrator.agent_remote = remote
    orchestrator.msg_handler = handler
    orchestrator.idle_loop = SimpleNamespace(resume=Mock())
    deleted = []
    remote_turns = []
    original_remote_handle = remote.handle

    async def track_remote_handle(msg, route=None):
        remote_turns.append(msg.text)
        return await original_remote_handle(msg, route)

    remote.handle = track_remote_handle
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kwargs: deleted.append(kwargs) or {
            "status": "deleted", "item": {"id": 5, "title": "Video"},
        },
    )

    async def run():
        await orchestrator.handle_message(
            _discord_message("codex: mantis analysiere README", message_id="agent-delete"),
        )
        handler._pending_inbox_deletions["123"] = PendingInboxDeletion(
            item_id=5,
            channel_id="discord-dm-123",
            prompt_message_id="delete-prompt",
            created_at=time.monotonic(),
        )
        await orchestrator.handle_message(
            _discord_message(
                "ja", message_id="delete-confirmation", reply_to_message_id="delete-prompt",
            ),
        )
        assert remote.pending == {}
        await remote.stop()

    asyncio.run(run())

    assert deleted == [{"item_id": 5}]
    assert remote_turns == ["codex: mantis analysiere README"]
    confirmations = [row for row in _persisted(rows, "user") if row[1] == "ja"]
    assert len(confirmations) == 1
    assert _meta(confirmations[0]) == {"use_case": "inbox"}
