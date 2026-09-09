"""Delegation integration and task-local resource limits, without network calls."""
import asyncio
import pytest

from core import tools as toolreg
from core.backends.ollama import OllamaBackend
from tools import delegate


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(delegate, "_active", {})
    monkeypatch.setattr("core.agent.log_event", lambda *args, **kwargs: None)


def test_subagent_uses_real_backend_and_enforces_blocked_tools(monkeypatch):
    effects = []

    async def forbidden():
        effects.append("sent")
        return "sent"

    monkeypatch.setitem(toolreg.REGISTRY, "send_telegram", toolreg.Tool(
        "send_telegram", "Send", {}, forbidden))

    async def call(self, messages, **kwargs):
        if messages[-1]["role"] == "tool":
            return messages[-1]["content"], []
        return "", [{"function": {"name": "send_telegram", "arguments": {}}}]

    monkeypatch.setattr(OllamaBackend, "call", call)
    result = asyncio.run(delegate.run_subagent("Send", allowed_tools=["send_telegram"]))
    assert "FEHLER" in result
    assert "Subagent-Fehler" not in result
    assert "Subagent-Ausführungsfehler" not in result
    assert effects == []


def test_subagent_executes_permitted_tool_with_real_agent(monkeypatch):
    effects = []

    async def record():
        effects.append("recorded")
        return "recorded"

    monkeypatch.setitem(toolreg.REGISTRY, "record", toolreg.Tool("record", "Record", {}, record))

    async def call(self, messages, **kwargs):
        if messages[-1]["role"] == "tool":
            return messages[-1]["content"], []
        return "", [{"function": {"name": "record", "arguments": {}}}]

    monkeypatch.setattr(OllamaBackend, "call", call)
    assert asyncio.run(delegate.run_subagent("Record", allowed_tools=["record"])) == "recorded"
    assert effects == ["recorded"]


def test_backend_failure_marks_subagent_as_error(monkeypatch):
    async def call(self, messages, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(OllamaBackend, "call", call)
    assert "backend unavailable" in asyncio.run(delegate.run_subagent("Work"))
    assert delegate.get_active_subagents()[0]["status"] == "error"


def test_independent_subagents_can_run_concurrently_up_to_limit(monkeypatch):
    async def scenario():
        started = []
        release = asyncio.Event()

        async def execute(sub_id, goal, context, allowed_tools):
            started.append(goal)
            await release.wait()
            return goal

        monkeypatch.setattr(delegate, "_execute_subagent", execute)
        tasks = [asyncio.create_task(delegate.run_subagent(str(i))) for i in range(3)]
        for _ in range(4):
            await asyncio.sleep(0)
        try:
            assert len(started) == 3
            assert "parallele" in await delegate.run_subagent("overflow")
        finally:
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_completed_subagents_do_not_consume_concurrency(monkeypatch):
    async def execute(*args):
        return "done"

    monkeypatch.setattr(delegate, "_execute_subagent", execute)

    async def scenario():
        for _ in range(5):
            assert await delegate.run_subagent("work") == "done"

    asyncio.run(scenario())


def test_recursive_delegation_remains_blocked(monkeypatch):
    async def execute(*args):
        return await delegate.run_subagent("nested")

    monkeypatch.setattr(delegate, "_execute_subagent", execute)
    assert "Tiefe" in asyncio.run(delegate.run_subagent("parent"))


def test_cancellation_releases_capacity_and_depth(monkeypatch):
    async def scenario():
        entered = asyncio.Event()

        async def execute(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(delegate, "_execute_subagent", execute)
        task = asyncio.create_task(delegate.run_subagent("cancel"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(row["status"] != "running" for row in delegate.get_active_subagents())

    asyncio.run(scenario())
