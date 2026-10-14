"""Ollama bleibt ausschließlich für den lokalen Embedding-Pfad verfügbar."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from llm.local import OllamaEmbeddingProvider, _embed_cache_key


class FakeOllamaClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []

    async def embed(self, **kwargs):
        self.calls.append(kwargs)
        texts = kwargs["input"]
        count = len(texts) if isinstance(texts, list) else 1
        return SimpleNamespace(embeddings=[[float(i), 1.0] for i in range(count)])


def test_embedding_model_uses_local_config_and_caches_single_embeddings(monkeypatch):
    from llm import local

    client = FakeOllamaClient()
    monkeypatch.setattr(local._ollama, "AsyncClient", lambda **kwargs: client)
    provider = OllamaEmbeddingProvider(embed_model="qwen3-embedding:0.6b")

    async def run():
        first = await provider.embed("  ein Text  ")
        second = await provider.embed("ein Text")
        return first, second

    first, second = asyncio.run(run())

    assert first == second == [0.0, 1.0]
    assert provider.model_name == "qwen3-embedding:0.6b"
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "qwen3-embedding:0.6b"
    assert client.calls[0]["input"] == "  ein Text  "


def test_embedding_batch_preserves_order_and_uses_one_local_call(monkeypatch):
    from llm import local

    client = FakeOllamaClient()
    monkeypatch.setattr(local._ollama, "AsyncClient", lambda **kwargs: client)
    provider = OllamaEmbeddingProvider(embed_model="qwen3-embedding:0.6b")

    result = asyncio.run(provider.embed_batch(["eins", "zwei"]))

    assert result == [[0.0, 1.0], [1.0, 1.0]]
    assert len(client.calls) == 1
    assert client.calls[0]["input"] == ["eins", "zwei"]
    assert client.calls[0]["model"] == "qwen3-embedding:0.6b"


def test_embedding_provider_exposes_no_generation_api():
    provider = OllamaEmbeddingProvider()

    assert not hasattr(provider, "chat")
    assert not hasattr(provider, "stream")
    assert not hasattr(provider, "complete")


def test_embed_cache_key_strips_and_truncates():
    assert _embed_cache_key("  hallo  ") == "hallo"
    assert len(_embed_cache_key("x" * 1000)) == 512
