import asyncio

import pytest

from core import voice_timing
from core.agent import Agent


class _Backend:
    model_name = "fake"

    def __init__(self):
        self.calls = 0

    async def call(self, messages, tools, stream_cb, temperature, max_tokens, **kw):
        self.calls += 1
        await asyncio.sleep(0.01)
        if stream_cb:
            await stream_cb("hallo")
        voice_timing.note_output_tokens(7)
        if self.calls == 1:
            return "", [{"id": "t1", "function": {"name": "calculate", "arguments": {}}}]
        return "fertig", []

    async def warmup(self):
        pass


class _FailingBackend:
    model_name = "fake"

    def __init__(self, error):
        self.error = error

    async def call(self, messages, tools, stream_cb, temperature, max_tokens, **kw):
        raise self.error

    async def warmup(self):
        pass


def test_snapshot_without_timing_has_zeroed_keys():
    snap = voice_timing.snapshot()
    assert snap["tool_select_ms"] == 0 and snap["context_ms"] == 0
    assert snap["tool_exec_ms"] == 0 and snap["llm_calls"] == []


def test_phase_accumulates():
    async def run():
        voice_timing.start()
        with voice_timing.phase("context_ms"):
            await asyncio.sleep(0.01)
        return voice_timing.snapshot()

    assert asyncio.run(run())["context_ms"] >= 9


def test_tool_exec_phase_accumulates_across_two_tools(monkeypatch):
    from core import tools as toolreg

    class TwoToolBackend:
        model_name = "fake"

        def __init__(self):
            self.calls = 0

        async def call(self, messages, tools, stream_cb, temperature, max_tokens, **kw):
            self.calls += 1
            if self.calls == 1:
                return "", [
                    {"id": "t1", "function": {"name": "calculate", "arguments": {}}},
                    {"id": "t2", "function": {"name": "calculate", "arguments": {}}},
                ]
            return "fertig", []

        async def warmup(self):
            pass

    async def fake_execute(_name, _args):
        await asyncio.sleep(0.01)
        return "ok"

    monkeypatch.setattr(toolreg, "execute", fake_execute)
    monkeypatch.setattr(
        toolreg, "ollama_schemas",
        lambda only=None: [{"type": "function", "function": {
            "name": "calculate", "description": "", "parameters": {"type": "object", "properties": {}},
        }}],
    )

    async def run():
        voice_timing.start()
        await Agent(TwoToolBackend()).run(
            messages=[{"role": "user", "content": "x"}], system="s",
            allowed_tools=["calculate"], max_tokens=50,
        )
        return voice_timing.snapshot()

    assert asyncio.run(run())["tool_exec_ms"] >= 18


def test_llm_call_is_recorded_when_backend_raises():
    timing_holder = []

    async def run():
        voice_timing.start()
        timing_holder.append(voice_timing.current())
        await Agent(_FailingBackend(RuntimeError("backend down")))._call_backend(
            [], tools=None, stream_cb=None, temperature=0.7, max_tokens=10,
        )

    with pytest.raises(RuntimeError, match="backend down"):
        asyncio.run(run())

    assert timing_holder[0].llm_calls[0]["error"] == "RuntimeError"


def test_llm_call_is_recorded_when_cancelled():
    timing_holder = []
    backend_started = None

    class BlockingBackend:
        model_name = "fake"

        async def call(self, messages, tools, stream_cb, temperature, max_tokens, **kw):
            backend_started.set()
            await asyncio.Event().wait()

        async def warmup(self):
            pass

    async def run():
        nonlocal backend_started
        backend_started = asyncio.Event()
        voice_timing.start()
        timing_holder.append(voice_timing.current())
        call = asyncio.create_task(Agent(BlockingBackend())._call_backend(
            [], tools=None, stream_cb=None, temperature=0.7, max_tokens=10,
        ))
        await backend_started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(run())

    assert timing_holder[0].llm_calls[0]["error"] == "CancelledError"


def test_context_reset_restores_previous_timing():
    outer_token = voice_timing.start()
    outer_timing = voice_timing.current()
    inner_token = voice_timing.start()

    assert voice_timing.current() is not outer_timing
    voice_timing.reset(inner_token)
    assert voice_timing.current() is outer_timing
    voice_timing.reset(outer_token)
    assert voice_timing.current() is None


def test_agent_records_llm_calls_and_tool_exec(monkeypatch):
    from core import tools as toolreg

    async def fake_execute(name, args):
        await asyncio.sleep(0.01)
        return "ok"

    monkeypatch.setattr(toolreg, "execute", fake_execute)
    monkeypatch.setattr(
        toolreg, "ollama_schemas",
        lambda only=None: [{"type": "function", "function": {
            "name": "calculate", "description": "", "parameters": {"type": "object", "properties": {}},
        }}],
    )

    async def run():
        voice_timing.start()
        agent = Agent(_Backend())

        async def cb(_event):
            pass

        await agent.run(
            messages=[{"role": "user", "content": "x"}], system="s", stream_cb=cb,
            allowed_tools=["calculate"], max_tokens=50,
        )
        return voice_timing.snapshot()

    snap = asyncio.run(run())
    assert len(snap["llm_calls"]) == 2
    first = snap["llm_calls"][0]
    assert set(first) == {"ms_to_first_token", "ms_total", "output_tokens", "had_tool_calls"}
    assert first["had_tool_calls"] is True and snap["llm_calls"][1]["had_tool_calls"] is False
    assert first["output_tokens"] == 7 and first["ms_total"] >= first["ms_to_first_token"] > 0
    assert snap["tool_exec_ms"] >= 9
