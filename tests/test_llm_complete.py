"""Cloud-Adapter implementiert complete() ohne lokale Generierungsstufe."""
from __future__ import annotations

import asyncio

from core.backends.base import AgentBackend
from core.backends.llm_adapter import BackendLLMAdapter
from llm.base import LLMProvider
from llm.routed import RoutedLLMProvider


class FakeBackend(AgentBackend):
    def __init__(self):
        self.calls = []

    @property
    def model_name(self):
        return "cloud-fixture"

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return "  Antwort  ", []

    async def warmup(self):
        return None


class FakeEmbeddingProvider:
    async def embed(self, _text):
        return [0.1]

    async def embed_batch(self, texts):
        return [[0.1] for _ in texts]


def test_backend_adapter_complete_wraps_prompt_and_forwards_options():
    backend = FakeBackend()
    provider = BackendLLMAdapter(backend)

    result = asyncio.run(provider.complete(
        "hallo", system="Du bist Mantis", max_tokens=300, temperature=0.2, format="json",
    ))

    assert result == "  Antwort  "
    assert backend.calls == [{
        "messages": [
            {"role": "system", "content": "Du bist Mantis\n\nAntworte ausschließlich mit validem JSON, ohne Text drumherum."},
            {"role": "user", "content": "hallo"},
        ],
        "tools": None,
        "temperature": 0.2,
        "max_tokens": 300,
    }]


def test_routed_complete_uses_one_background_cloud_chain_for_all_route_types(monkeypatch):
    from llm import routed

    backend = FakeBackend()
    purposes = []

    def cloud_chain(purpose, **kwargs):
        purposes.append((purpose, kwargs))
        return backend

    monkeypatch.setattr(routed, "cloud_chain", cloud_chain)
    provider = RoutedLLMProvider(embedding_provider=FakeEmbeddingProvider())

    asyncio.run(provider.complete("KZG-Zusammenfassung"))
    asyncio.run(provider.complete("Erstelle eine Python-Funktion"))
    asyncio.run(provider.complete("Erstelle das Morgen-Briefing"))

    assert purposes == [("background", {"reasoning": "low"})]
    assert [call["temperature"] for call in backend.calls] == [0.7, 0.6, 0.7]
    assert all(call["tools"] is None for call in backend.calls)


def test_complete_remains_part_of_the_provider_interface():
    assert callable(getattr(LLMProvider, "complete", None))
