"""Fast-Klassifikation läuft über dieselbe Cloud-Kette wie die übrigen Zwecke."""
import asyncio

from core.backends.base import AgentBackend
from core import fast


class FakeCloudBackend(AgentBackend):
    def __init__(self):
        self.calls = []

    @property
    def model_name(self):
        return "fake-cloud"

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls.append({
            "messages": messages, "tools": tools, "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return "JA", []

    async def warmup(self):
        return None


def test_fast_ask_uses_fast_cloud_chain_and_forwards_json_format(monkeypatch):
    backend = FakeCloudBackend()
    setup = []
    local_calls = []

    def cloud_chain(purpose, **kwargs):
        setup.append((purpose, kwargs))
        return backend

    class LocalClient:
        async def chat(self, **kwargs):
            local_calls.append(kwargs)
            return type("Response", (), {"message": type("Message", (), {"content": "JA"})()})()

    monkeypatch.setattr(fast, "cloud_chain", cloud_chain, raising=False)
    monkeypatch.setattr(fast, "_client", LocalClient(), raising=False)
    monkeypatch.setattr(fast, "_provider", None, raising=False)

    answer = asyncio.run(fast.ask("Klassifiziere", max_tokens=9, format="json"))

    assert answer == "JA"
    assert setup == [("fast", {
        "reasoning": "minimal", "timeout": 3, "total_timeout": 9,
    })]
    assert backend.calls[0]["max_tokens"] == 9
    assert "Antworte ausschließlich mit validem JSON" in backend.calls[0]["messages"][0]["content"]
    assert local_calls == []
