import asyncio

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
