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


def test_interrupt_flag_prevents_agent_tool_dispatch(echo_tool, monkeypatch):
    calls = []

    async def execute_echo(text=""):
        calls.append(text)
        return f"ECHO:{text}"

    monkeypatch.setattr(toolreg.REGISTRY["echo"], "handler", execute_echo)
    backend = FakeBackend([("", [_tc("echo", {"text": "must not run"})])])

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await Agent(backend).run(
                [{"role": "user", "content": "Licht aus"}], system="s",
                interrupt_check=lambda: True,
            )

    asyncio.run(run())
    assert calls == []


def test_interrupt_after_agent_step_prevents_tool_start(echo_tool, monkeypatch):
    calls = []
    interrupted = False

    async def execute_echo(text=""):
        calls.append(text)
        return f"ECHO:{text}"

    async def interrupt_at_tool_start(event):
        nonlocal interrupted
        if event.kind == "tool_start":
            interrupted = True

    monkeypatch.setattr(toolreg.REGISTRY["echo"], "handler", execute_echo)
    backend = FakeBackend([("", [_tc("echo", {"text": "must not run"})])])

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await Agent(backend).run(
                [{"role": "user", "content": "Licht aus"}], system="s",
                stream_cb=interrupt_at_tool_start,
                interrupt_check=lambda: interrupted,
            )

    asyncio.run(run())
    assert interrupted is True
    assert calls == []


def test_stream_events_mark_tool_rounds_as_nonfinal(echo_tool, monkeypatch):
    class StreamingBackend(FakeBackend):
        def __init__(self):
            super().__init__([("", [_tc("echo", {"text": "hi"})]), ("Fertig.", [])])
            self.updates = [["Zwischenstand"], ["Fertig."]]

        async def call(self, messages, tools=None, stream_cb=None,
                       temperature=0.7, max_tokens=1500, think=False,
                       force_tool_call=False):
            self.calls.append({"messages": list(messages), "tools": tools})
            for update in self.updates[len(self.calls) - 1]:
                await stream_cb(update)
            return self.responses.pop(0)

    backend = StreamingBackend()
    received = []
    timeline = []

    async def execute_echo(text=""):
        timeline.append("execute")
        return f"ECHO:{text}"

    monkeypatch.setattr(toolreg.REGISTRY["echo"], "handler", execute_echo)

    async def collect(update):
        received.append(update)
        if update.kind == "tool_start":
            timeline.append("tool_start")

    asyncio.run(Agent(backend).run(
        [{"role": "user", "content": "hi"}], system="s", stream_cb=collect,
    ))

    assert [event.kind for event in received] == [
        "attempt_start", "delta", "attempt_end",
        "tool_start",
        "attempt_start", "delta", "attempt_end",
    ]
    assert next(event for event in received if event.kind == "tool_start").tool_name == "echo"
    assert timeline == ["tool_start", "execute"]
    assert [event.text_delta for event in received if event.kind == "delta"] == [
        "Zwischenstand", "Fertig.",
    ]
    starts = [event for event in received if event.kind == "attempt_start"]
    assert starts[0].attempt_id != starts[1].attempt_id
    assert [event.final for event in received if event.kind == "attempt_end"] == [False, True]


def test_math_guard_retry_streams_with_a_new_attempt_id(echo_tool):
    class StreamingBackend(FakeBackend):
        def __init__(self):
            super().__init__([
                ("Der Mittelwert ~ 73 pro Tag", []),
                ("Die geprüfte Antwort ist 73.", []),
            ])
            self.stream_callbacks = []

        async def call(self, messages, tools=None, stream_cb=None,
                       temperature=0.7, max_tokens=1500, think=False,
                       force_tool_call=False):
            self.stream_callbacks.append(stream_cb)
            response = self.responses.pop(0)
            if stream_cb is not None:
                await stream_cb(response[0])
            return response

    backend = StreamingBackend()
    events = []

    async def collect(event):
        events.append(event)

    text, _ = asyncio.run(Agent(backend).run(
        [{"role": "user", "content": "rechne"}], system="s", stream_cb=collect,
    ))

    assert text == "Die geprüfte Antwort ist 73."
    assert all(callback is not None for callback in backend.stream_callbacks)
    starts = [event for event in events if getattr(event, "kind", None) == "attempt_start"]
    assert len(starts) == 2
    assert len({event.attempt_id for event in starts}) == 2
    assert [(event.kind, event.final) for event in events if getattr(event, "kind", None) == "attempt_end"] == [
        ("attempt_end", False), ("attempt_end", True),
    ]


def test_tool_round_streams_have_distinct_attempt_ids(echo_tool):
    class StreamingBackend(FakeBackend):
        async def call(self, messages, tools=None, stream_cb=None,
                       temperature=0.7, max_tokens=1500, think=False,
                       force_tool_call=False):
            self.calls.append({"messages": list(messages), "tools": tools})
            response = self.responses.pop(0)
            if stream_cb is not None:
                await stream_cb("Suche abgeschlossen." if response[1] else response[0])
            return response

    backend = StreamingBackend([
        ("", [_tc("echo", {"text": "hi"})]),
        ("Fertig.", []),
    ])
    events = []

    async def collect(event):
        events.append(event)

    asyncio.run(Agent(backend).run(
        [{"role": "user", "content": "x"}], system="s", stream_cb=collect,
    ))

    starts = [event for event in events if getattr(event, "kind", None) == "attempt_start"]
    assert len(starts) == 2
    assert len({event.attempt_id for event in starts}) == 2
    assert [(event.kind, event.final) for event in events if getattr(event, "kind", None) == "attempt_end"] == [
        ("attempt_end", False), ("attempt_end", True),
    ]


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


def test_deferred_tool_selection_blocks_unpermitted_tool_after_selection_finishes(echo_tool):
    executed = []

    async def write(text=""):
        executed.append(text)
        return "written"

    toolreg.REGISTRY["echo"].handler = write
    be = FakeBackend([("", [_tc("echo", {"text": "unauthorized"})]), ("Done", [])])

    async def run():
        release_selection = asyncio.Event()

        async def selection():
            await release_selection.wait()
            return ["calculate"], False

        selection_task = asyncio.create_task(selection())
        agent_task = asyncio.create_task(Agent(be).run(
            [], system="s", tool_selection_task=selection_task,
        ))
        await asyncio.sleep(0)
        assert len(be.calls) == 1
        assert not agent_task.done()
        release_selection.set()
        return await agent_task

    _text, trace = asyncio.run(run())
    assert executed == []
    assert "nicht erlaubt" in trace[0]["result"]


def test_deferred_tool_selection_does_not_delay_answer_without_tool(echo_tool):
    be = FakeBackend([("Direkte Antwort.", [])])

    async def run():
        pending_selection = asyncio.get_running_loop().create_future()
        result = await Agent(be).run(
            [], system="s", tool_selection_task=pending_selection,
        )
        assert not pending_selection.done()
        return result

    assert asyncio.run(run()) == ("Direkte Antwort.", [])


@pytest.mark.parametrize("selection_ready", [True, False], ids=["fertig", "verzoegert"])
def test_use_tools_false_stays_a_global_lock_after_deferred_selection(echo_tool, monkeypatch, selection_ready):
    executed = []

    async def write(text=""):
        executed.append(text)
        return "written"

    monkeypatch.setattr(toolreg.REGISTRY["echo"], "handler", write)
    be = FakeBackend([("", [_tc("echo", {"text": "unauthorized"})]), ("Done", [])])

    async def run():
        loop = asyncio.get_running_loop()
        if selection_ready:
            selection_task = loop.create_future()
            selection_task.set_result((["echo"], True))
        else:
            async def select_later():
                await asyncio.sleep(0)
                return ["echo"], True

            selection_task = asyncio.create_task(select_later())
        try:
            result = await Agent(be).run(
                [], system="s", use_tools=False, tool_selection_task=selection_task,
            )
            if not selection_ready:
                await selection_task
            return result
        finally:
            if not selection_ready and not selection_task.done():
                selection_task.cancel()
                await asyncio.gather(selection_task, return_exceptions=True)

    _, trace = asyncio.run(run())
    assert executed == []
    assert "nicht erlaubt" in trace[0]["result"]


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
