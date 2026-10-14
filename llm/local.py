"""Lokale Ollama-Embeddings für LZG; Textgenerierung läuft über Cloud-Backends."""
from __future__ import annotations

import logging
from collections import OrderedDict

import ollama as _ollama

from core.llm_gate import GATE
import config

log = logging.getLogger(__name__)


def _embed_cache_key(text: str) -> str:
    return text.strip()[:512]


class OllamaEmbeddingProvider:
    """Lokaler Provider ausschließlich für Text-Embeddings."""

    def __init__(self, embed_model: str | None = None):
        self._embed_model = embed_model or config.LZG_EMBED_MODEL
        self._client = _ollama.AsyncClient(host=config.OLLAMA_BASE_URL)
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_cache_max = 256

    @property
    def model_name(self) -> str:
        return self._embed_model

    async def embed(self, text: str) -> list[float]:
        key = _embed_cache_key(text)
        if key in self._embed_cache:
            return self._embed_cache[key]
        async with GATE:
            response = await self._client.embed(
                model=self._embed_model,
                input=text,
                options=config.ollama_options(self._embed_model),
            )
        embedding = response.embeddings[0]
        if len(self._embed_cache) >= self._embed_cache_max:
            self._embed_cache.popitem(last=False)
        self._embed_cache[key] = embedding
        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch-Embedding: mehrere Texte in einem Ollama-Call (spart Round-Trips)."""
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for index, text in enumerate(texts):
            key = _embed_cache_key(text)
            if key in self._embed_cache:
                self._embed_cache.move_to_end(key)
                results[index] = self._embed_cache[key]
            else:
                uncached_indices.append(index)
                uncached_texts.append(text)

        if uncached_texts:
            async with GATE:
                response = await self._client.embed(
                    model=self._embed_model,
                    input=uncached_texts,
                    options=config.ollama_options(self._embed_model),
                )
            for index, (original_index, text) in enumerate(zip(uncached_indices, uncached_texts)):
                embedding = response.embeddings[index]
                key = _embed_cache_key(text)
                if len(self._embed_cache) >= self._embed_cache_max:
                    self._embed_cache.popitem(last=False)
                self._embed_cache[key] = embedding
                results[original_index] = embedding

        return [result for result in results if result is not None]

    async def check_model(self) -> bool:
        """Prüft, ob das Embedding-Modell lokal verfügbar ist."""
        try:
            models = await self._client.list()
            names = [model.model for model in models.models]
            return any(self._embed_model in name for name in names)
        except Exception:
            return False

    async def pull_if_missing(self) -> None:
        """Lädt ausschließlich das konfigurierte Embedding-Modell herunter."""
        if await self.check_model():
            return
        log.info("📥 Lade Embedding-Modell %s...", self._embed_model)
        await self._client.pull(self._embed_model)
        log.info("✅ Embedding-Modell %s bereit", self._embed_model)


# Bestehende Imports bleiben für externe Integrationen lesbar, die Klasse hat
# aber bewusst keine chat()/stream()-Methoden und kann kein Modell generieren.
OllamaProvider = OllamaEmbeddingProvider
