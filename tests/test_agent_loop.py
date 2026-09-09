"""Tests für den ReAct-Loop (core/agent.py) mit Fake-Backend — kein LLM nötig.

Deckt insbesondere den Math-Guard ab: dessen Retry-Tool-Calls müssen AUSGEFÜHRT
werden statt verworfen (der alte `continue`-Pfad machte einen zweiten LLM-Call
und warf die Tool-Calls weg).
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tools as toolreg
from core.agent import Agent


class FakeBackend:
    """Gibt vorbereitete (content, tool_calls)-Antworten der Reihe nach zurück."""

    model_name = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []  # aufgezeichnete call()-Argumente

    async def call(self, messages, tools=None, stream_cb=None,
                   temperature=0.7, max_tokens=1500, think=False,
                   force_tool_call=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            return "LEER", []
        return self.responses.pop(0)

    async def warmup(self):
        pass


def _tc(name, args=None):
    return {"function": {"name": name, "arguments": args or {}}}


@pytest.fixture
def echo_tool():
    """Registriert ein Dummy-Tool 'echo' und 'calculate' für die Loop-Tests."""
    async def _echo(text: str = ""):
        return f"ECHO:{text}"

    async def _calc(expression: str = ""):
        return "42"

    saved = dict(toolreg.REGISTRY)
    toolreg.REGISTRY["echo"] = toolreg.Tool("echo", "Echo", {"text": {"type": "string"}}, _echo)
    toolreg.REGISTRY["calculate"] = toolreg.Tool(
        "calculate", "Rechnet", {"expression": {"type": "string"}}, _calc)
    yield
    toolreg.REGISTRY.clear()
    toolreg.REGISTRY.update(saved)


@pytest.fixture(autouse=True)
def no_production_event_log(monkeypatch):
    monkeypatch.setattr("core.agent.log_event", lambda *args, **kwargs: None)


def test_plain_answer_no_tools(echo_tool):
    be = FakeBackend([("Hallo Timo!", [])])
    text, trace = asyncio.run(Agent(be).run([{"role": "user", "content": "hi"}], system="s"))
    assert text == "Hallo Timo!"
    assert trace == []
    assert len(be.calls) == 1


def test_tool_call_then_answer(echo_tool):
    be = FakeBackend([
        ("", [_tc("echo", {"text": "hi"})]),
        ("Fertig.", []),
    ])
    text, trace = asyncio.run(Agent(be).run([{"role": "user", "content": "x"}], system="s"))
    assert text == "Fertig."
    assert [t["tool"] for t in trace] == ["echo"]
    assert trace[0]["result"] == "ECHO:hi"
    # Tool-Ergebnis muss beim Folgecall im Kontext liegen
    roles = [m["role"] for m in be.calls[1]["messages"]]
    assert "tool" in roles


def test_math_guard_executes_retry_tool_calls(echo_tool):
    """Math-Guard-Retry liefert calculate → muss ausgeführt werden, ohne Extra-LLM-Call."""
    be = FakeBackend([
        ("Der Mittelwert ~ 73 pro Tag", []),           # Rechnung ohne Tool → Guard
        ("", [_tc("calculate", {"expression": "511/7"})]),  # Guard-Retry liefert Tool-Call
        ("Es sind 42.", []),                            # Antwort nach Tool-Ergebnis
    ])
    text, trace = asyncio.run(Agent(be).run([{"role": "user", "content": "rechne"}], system="s"))
    assert text == "Es sind 42."
    assert [t["tool"] for t in trace] == ["calculate"]
    assert len(be.calls) == 3  # KEIN vierter Call — Retry-Tool-Calls direkt ausgeführt


def test_math_guard_gives_up_without_tool_call(echo_tool):
    be = FakeBackend([
        ("Durchschnitt ~ 73", []),
        ("Durchschnitt ~ 73, ohne Tool.", []),  # Retry liefert wieder keinen Call
    ])
    text, trace = asyncio.run(Agent(be).run([{"role": "user", "content": "x"}], system="s"))
    assert "73" in text
    assert trace == []
    assert len(be.calls) == 2


def test_force_tools_retry(echo_tool):
    be = FakeBackend([
        ("rede nur", []),                       # kein Tool trotz force
        ("", [_tc("echo", {"text": "ok"})]),    # Retry liefert Tool-Call
        ("Fertig.", []),
    ])
    text, trace = asyncio.run(Agent(be).run(
        [{"role": "user", "content": "x"}], system="s", force_tools=True))
    assert text == "Fertig."
    assert [t["tool"] for t in trace] == ["echo"]


def test_max_steps_final_answer_without_tools(echo_tool):
    # Backend will endlos Tools aufrufen → nach max_steps letzter Call OHNE Tools
    be = FakeBackend(
        [("", [_tc("echo", {"text": "loop"})])] * 3 + [("Abschluss.", [])]
    )
    text, trace = asyncio.run(Agent(be, max_steps=3).run(
        [{"role": "user", "content": "x"}], system="s"))
    assert text == "Abschluss."
    assert len(trace) == 3
    assert be.calls[-1]["tools"] is None  # finaler Call ohne Tool-Schemas


def test_dry_run_does_not_execute(echo_tool):
    be = FakeBackend([
        ("", [_tc("echo", {"text": "hi"})]),
        ("Fertig.", []),
    ])
    _, trace = asyncio.run(Agent(be).run(
        [{"role": "user", "content": "x"}], system="s", dry_run_tools=True))
    assert "Dry-Run" in trace[0]["result"]  # nicht wirklich ausgeführt


@pytest.mark.parametrize("permissions", [
    {"allowed_tools": ["calculate"]},
    {"allowed_tools": []},
    {"use_tools": False},
    {"use_tools": False, "allowed_tools": ["echo"]},
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_backend_cannot_execute_unpermitted_tools(echo_tool, monkeypatch, permissions, dry_run):
    executed = []

    async def write(text=""):
        executed.append(text)
        return "written"

    monkeypatch.setattr(toolreg.REGISTRY["echo"], "handler", write)
    be = FakeBackend([("", [_tc("echo", {"text": "unauthorized"})]), ("Done", [])])
    _, trace = asyncio.run(Agent(be).run([], system="s", dry_run_tools=dry_run, **permissions))
    assert executed == []
    assert "FEHLER" in trace[0]["result"]
    assert "Dry-Run" not in trace[0]["result"]
    assert be.calls[1]["messages"][-1]["content"] == trace[0]["result"]


def test_math_guard_does_not_request_disallowed_calculate(echo_tool):
    be = FakeBackend([("Durchschnitt ~ 73", [])])
    text, trace = asyncio.run(Agent(be).run([], system="s", allowed_tools=["echo"]))
    assert text == "Durchschnitt ~ 73"
    assert trace == []
    assert len(be.calls) == 1


def test_unknown_tool_is_rejected_in_dry_run(echo_tool):
    be = FakeBackend([("", [_tc("nonexistent")]), ("Done", [])])
    _, trace = asyncio.run(Agent(be).run([], system="s", dry_run_tools=True))
    assert "FEHLER" in trace[0]["result"]
