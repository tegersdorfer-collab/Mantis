"""Tool-Grenze: nur die eigene Inbox, vorsichtige Mutationen, keine Inhaltsmetriken."""

import asyncio
import logging
from datetime import datetime

from core import db, tools
from core.skill_context import CTX
from core.skills import inbox as inbox_skills


def test_search_tool_uses_inbox_filters_local_embedding_and_logs_no_query(monkeypatch):
    calls = []
    events = []

    class Embedder:
        async def embed(self, text):
            calls.append(("embed", text))
            return [0.25] * 1024

    def search(spec, *, embedding, limit):
        calls.append(("search", spec, embedding, limit))
        return [{
            "id": 7, "title": "Balkonbeet", "category": "idee", "status": "open",
            "created_at": datetime(2026, 10, 13), "source_urls": ["https://example.org/plan"],
        }]

    monkeypatch.setattr(CTX, "llm", Embedder())
    monkeypatch.setattr(inbox_skills.store, "search_items", search)
    monkeypatch.setattr(db, "log_event", lambda *args: events.append(args))

    result = asyncio.run(tools.execute("inbox_search", {
        "query": "Balkon", "category": "idee", "status": "open", "has_url": True,
    }))

    assert result == "ID 7 · Balkonbeet · Idee · 13.10.2026 · offen · https://example.org/plan"
    assert calls[0] == ("embed", "Balkon")
    assert calls[1][1].category == "idee"
    assert calls[1][1].has_url is True
    assert calls[1][2] == [0.25] * 1024
    assert len(events) == 1
    assert events[0][:2] == ("inbox", "search")
    assert events[0][2]["status"] == "found"
    assert events[0][2]["count"] == 1
    assert events[0][2]["duration_ms"] >= 0
    assert "Balkon" not in repr(events)


def test_search_tool_normalizes_natural_language_before_its_sql_query(monkeypatch):
    captured = []
    monkeypatch.setattr(CTX, "llm", None)
    monkeypatch.setattr(
        inbox_skills.store, "search_items",
        lambda spec, **_kwargs: captured.append(spec) or [],
    )
    monkeypatch.setattr(db, "log_event", lambda *_a, **_k: None)

    result = asyncio.run(tools.execute("inbox_search", {
        "query": "Was wollte ich mir noch anschauen?",
    }))

    assert result == "Nichts gefunden."
    assert captured[0].query == ""
    assert captured[0].category == "anschauen"
    assert captured[0].status == "open"

    asyncio.run(tools.execute("inbox_search", {"query": "Zeig mir erledigte Merker"}))
    assert captured[1].query == ""
    assert captured[1].status == "done"


def test_ambiguous_completion_and_delete_ask_before_mutation(monkeypatch):
    events = []
    monkeypatch.setattr(inbox_skills.store, "mark_done", lambda **_kw: {
        "status": "ambiguous", "item": None,
    })
    monkeypatch.setattr(inbox_skills.store, "delete_item", lambda **_kw: {
        "status": "ambiguous", "item": None,
    })
    monkeypatch.setattr(db, "log_event", lambda *args: events.append(args))

    completed = inbox_skills._inbox_complete()
    deleted = inbox_skills._inbox_delete(query="Arrival")

    assert "Welchen Merker" in completed
    assert "Mehrere Merker" in deleted
    assert [event[:2] for event in events] == [
        ("inbox", "complete"), ("inbox", "delete"),
    ]
    assert all(event[2]["status"] == "ambiguous" for event in events)
    assert all(event[2]["duration_ms"] >= 0 for event in events)


def test_correction_passes_only_fields_named_by_the_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(inbox_skills.store, "resolve_item", lambda **_kw: {
        "status": "found", "item": {"id": 23, "title": "Arrival"},
    })
    monkeypatch.setattr(inbox_skills.store, "correct_item", lambda item_id, **fields: (
        calls.append((item_id, fields)) or {"id": item_id, "title": "Arrival (2016)"}
    ))
    monkeypatch.setattr(db, "log_event", lambda *_a, **_k: None)

    result = inbox_skills._inbox_correct(item_id=23, title="Arrival (2016)")

    assert result == "✏️ Korrigiert: Arrival (2016) · ID 23"
    assert calls == [(23, {"title": "Arrival (2016)"})]


def test_registered_tools_use_the_inbox_category_only():
    names = {name for name, tool in tools.REGISTRY.items() if tool.category == "inbox"}

    assert names == {"inbox_search", "inbox_complete", "inbox_correct", "inbox_delete"}
    assert all(name not in names for name in ("brain_search", "brain_inbox"))


def test_agent_tool_telemetry_does_not_store_inbox_args_or_results(monkeypatch):
    from core.agent import Agent

    class Backend:
        model_name = "test-model"

        def __init__(self):
            self.calls = 0

        async def call(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return "", [{
                    "id": "call-1",
                    "function": {
                        "name": "inbox_search",
                        "arguments": {"query": "secret-topic"},
                    },
                }]
            return "Found https://example.org/private", []

        async def warmup(self):
            return None

    events = []
    monkeypatch.setattr("core.agent.log_event", lambda *args: events.append(args))
    monkeypatch.setattr(
        tools, "execute", __import__("unittest.mock").mock.AsyncMock(
            return_value="Found https://example.org/private"
        ),
    )

    asyncio.run(Agent(Backend()).run(
        messages=[], system="", allowed_tools=["inbox_search"],
    ))

    tool_events = [event for event in events if event[0] == "tool"]
    assert len(tool_events) == 1
    assert tool_events[0][1] == "inbox_search"
    assert tool_events[0][2] == {"status": "completed"}
    assert "secret-topic" not in repr(tool_events)
    assert "https://example.org/private" not in repr(tool_events)


def test_tool_failure_logs_only_error_type_for_inbox_queries(monkeypatch, caplog):
    monkeypatch.setattr(CTX, "llm", None)
    monkeypatch.setattr(
        inbox_skills.store, "search_items",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("secret-topic https://example.org/private")
        ),
    )
    monkeypatch.setattr(db, "log_event", lambda *_a, **_k: None)

    with caplog.at_level(logging.WARNING, logger="core.tools"):
        asyncio.run(tools.execute("inbox_search", {"query": "secret-topic"}))

    assert "secret-topic" not in caplog.text
    assert "https://example.org/private" not in caplog.text
