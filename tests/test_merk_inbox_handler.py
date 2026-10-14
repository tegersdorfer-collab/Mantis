"""Fast-Path- und Messbarkeitsvertrag am gemeinsamen Nachrichtenpfad."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from communication.base import IncomingMessage
from core import db
from core.message_handler import MessageHandler, _explicit_inbox_item_id


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


def test_explicit_inbox_id_is_extracted_outside_urls_only():
    assert _explicit_inbox_item_id("Lösch Merker ID 42") == 42
    assert _explicit_inbox_item_id("Lösch #43") == 43
    assert _explicit_inbox_item_id("Lösch diesen Link https://example.org/item#99") is None
    assert _explicit_inbox_item_id(
        "Lösch Merker https://example.org/a https://example.org/a#42"
    ) is None
    assert _explicit_inbox_item_id("Lösch Eintrag ID 99999999999999999999") is None


def test_search_with_negated_delete_is_never_routed_to_a_delete_tool(monkeypatch):
    handler, _rows, agent, _channel = _handler(monkeypatch)
    selected = []
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kw: deleted.append(kw) or {
            "status": "deleted", "item": {"id": 42, "title": "Arrival"},
        },
    )
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(side_effect=lambda text: selected.append(text) or (["inbox_search"], True)),
    )
    message = IncomingMessage(
        "Zeig mir Merker #42; auf keinen Fall löschen.", "123",
        datetime(2026, 10, 14), channel="discord", source_message_id="search-42",
    )

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    assert agent.run.await_count == 1
    assert agent.run.await_args.kwargs["allowed_tools"] == ["inbox_search"]
    assert agent.run.await_args.kwargs["force_tools"] is True
    assert selected == []  # Inbox-Management erzwingt das sichere Suchwerkzeug direkt.
    assert deleted == []


@pytest.mark.parametrize("text", [
    "Lösch Merker #42 bitte",
    "Lösch Merker https://example.org/a https://example.org/a#42",
    "Zeig mir Merker #42; auf keinen Fall löschen.",
])
def test_delete_like_message_with_extra_text_or_url_never_starts_delete(monkeypatch, text):
    handler, _rows, agent, _channel = _handler(monkeypatch)
    resolved = []
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.resolve_item",
        lambda **kw: resolved.append(kw),
    )
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kw: deleted.append(kw),
    )
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=(["inbox_search"], True)),
    )
    message = IncomingMessage(
        text, "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="unsafe-delete-like",
    )

    asyncio.run(handler.handle(message, resume_idle_cb=None))

    assert resolved == []
    assert deleted == []
    if "merker" in text.casefold():
        assert agent.run.await_args.kwargs["allowed_tools"] == ["inbox_search"]


def test_exact_id_delete_prompts_and_executes_only_after_next_ja(monkeypatch):
    handler, _rows, agent, channel = _handler(monkeypatch)
    resolved = []
    deleted = []
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_item", lambda **kw: (
        resolved.append(kw) or {"status": "found", "item": {"id": 42, "title": "Arrival"}}
    ))
    monkeypatch.setattr("core.message_handler.inbox_service.store.delete_item", lambda **kw: (
        deleted.append(kw) or {"status": "deleted", "item": {"id": 42, "title": "Arrival"}}
    ))
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(side_effect=AssertionError("Löschen darf keinen Agentenaufruf starten")),
    )
    request = IncomingMessage(
        "Lösch Merker #42", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="delete-request-42",
    )

    asyncio.run(handler.handle(request, resume_idle_cb=None))

    assert resolved == [{"item_id": 42}]
    assert deleted == []
    assert "🗑️ „Arrival“ löschen? ja/nein" in channel.send.await_args.args[0]

    confirmation = IncomingMessage(
        "ja", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="delete-yes-42",
    )
    asyncio.run(handler.handle(confirmation, resume_idle_cb=None))

    assert deleted == [{"item_id": 42}]
    assert agent.run.await_count == 0


def test_next_nein_cancels_pending_delete(monkeypatch):
    handler, _rows, agent, channel = _handler(monkeypatch)
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_item", lambda **_kw: {
        "status": "found", "item": {"id": 42, "title": "Arrival"},
    })
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kw: deleted.append(kw),
    )
    request = IncomingMessage(
        "Lösch #42", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="delete-request-cancel",
    )
    answer = IncomingMessage(
        "nein", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="delete-cancel",
    )

    asyncio.run(handler.handle(request, resume_idle_cb=None))
    asyncio.run(handler.handle(answer, resume_idle_cb=None))

    assert deleted == []
    assert handler._pending_inbox_deletions == {}
    assert agent.run.await_count == 0
    assert channel.send.await_args.args[0] == "Löschung abgebrochen."


def test_an_intervening_discord_message_expires_delete_confirmation(monkeypatch):
    handler, _rows, agent, _channel = _handler(monkeypatch)
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_item", lambda **_kw: {
        "status": "found", "item": {"id": 42, "title": "Arrival"},
    })
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kw: deleted.append(kw),
    )
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=([], False)),
    )
    request = IncomingMessage(
        "Lösch #42", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="delete-request-interrupted",
    )
    interlude = IncomingMessage(
        "Hallo", "456", datetime(2026, 10, 14), channel="discord",
        source_message_id="discord-interlude",
    )
    late_answer = IncomingMessage(
        "ja", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="late-delete-yes",
    )

    asyncio.run(handler.handle(request, resume_idle_cb=None))
    asyncio.run(handler.handle(interlude, resume_idle_cb=None))
    asyncio.run(handler.handle(late_answer, resume_idle_cb=None))

    assert deleted == []
    assert handler._pending_inbox_deletions == {}
    assert agent.run.await_count == 2


def test_delete_is_not_confirmed_before_prompt_send_finishes(monkeypatch):
    handler, _rows, agent, channel = _handler(monkeypatch)
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_item", lambda **_kw: {
        "status": "found", "item": {"id": 42, "title": "Arrival"},
    })
    deleted = []
    monkeypatch.setattr(
        "core.message_handler.inbox_service.store.delete_item",
        lambda **kw: deleted.append(kw) or {
            "status": "deleted", "item": {"id": 42, "title": "Arrival"},
        },
    )
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=([], False)),
    )

    async def run():
        prompt_started = asyncio.Event()
        release_prompt = asyncio.Event()

        async def delayed_send(text):
            if "löschen? ja/nein" in text:
                prompt_started.set()
                await release_prompt.wait()
            return None

        channel.send = delayed_send
        request = IncomingMessage(
            "Lösch #42", "123", datetime(2026, 10, 14), channel="discord",
            source_message_id="delete-request-prompt-pending",
        )
        early_answer = IncomingMessage(
            "ja", "123", datetime(2026, 10, 14), channel="discord",
            source_message_id="delete-yes-before-prompt-complete",
        )
        prompt_task = asyncio.create_task(handler.handle(request, resume_idle_cb=None))
        await prompt_started.wait()
        await handler.handle(early_answer, resume_idle_cb=None)
        release_prompt.set()
        await prompt_task

    asyncio.run(run())

    assert deleted == []
    assert handler._pending_inbox_deletions == {}
    assert agent.run.await_count == 1


def test_delete_reply_requires_a_merker_confirmation_reference(monkeypatch):
    handler, _rows, agent, channel = _handler(monkeypatch)
    lookups = []
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_confirmation_reference", lambda ref: (
        lookups.append(ref) or {"status": "not_found", "item": None}
    ))
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(return_value=(["inbox_search"], True)),
    )
    request = IncomingMessage(
        "Lösch das bitte", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="reply-delete", reply_to_message_id="ordinary-source-message",
    )

    asyncio.run(handler.handle(request, resume_idle_cb=None))

    assert lookups == ["ordinary-source-message"]
    assert channel.send.await_count == 1  # nur der normale Inbox-Agent antwortet
    assert agent.run.await_count == 1
    assert agent.run.await_args.kwargs["allowed_tools"] == ["inbox_search"]


def test_reply_to_confirmation_prompts_but_does_not_delete_immediately(monkeypatch):
    handler, _rows, agent, channel = _handler(monkeypatch)
    lookups = []
    monkeypatch.setattr("core.message_handler.inbox_service.store.resolve_confirmation_reference", lambda ref: (
        lookups.append(ref) or {"status": "found", "item": {"id": 7, "title": "Dune"}}
    ))
    deleted = []
    monkeypatch.setattr("core.message_handler.inbox_service.store.delete_item", lambda **kw: (
        deleted.append(kw) or {"status": "deleted", "item": {"id": 7, "title": "Dune"}}
    ))
    monkeypatch.setattr(
        "core.message_handler.skills.T.select_tools_async",
        AsyncMock(side_effect=AssertionError("Löschen darf keinen Agentenaufruf starten")),
    )
    request = IncomingMessage(
        "Lösch das bitte", "123", datetime(2026, 10, 14), channel="discord",
        source_message_id="reply-delete-confirmed", reply_to_message_id="confirmation-7",
    )

    asyncio.run(handler.handle(request, resume_idle_cb=None))

    assert lookups == ["confirmation-7"]
    assert deleted == []
    assert "🗑️ „Dune“ löschen? ja/nein" in channel.send.await_args.args[0]
    assert agent.run.await_count == 0
