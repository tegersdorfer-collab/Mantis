"""
Ollama Provider – lokale LLM-Inference.
Modell über OLLAMA_MODEL in .env konfigurierbar.
"""
import logging
from collections import OrderedDict
from typing import AsyncIterator
import ollama as _ollama

from .base import LLMProvider, Message
from core.llm_gate import GATE
import config

log = logging.getLogger(__name__)


def _embed_cache_key(text: str) -> str:
    return text.strip()[:512]


class OllamaProvider(LLMProvider):
    def __init__(self, model: str | None = None, embed_model: str | None = None):
        self._model = model or config.OLLAMA_MODEL
        self._embed_model = embed_model or config.LZG_EMBED_MODEL
        self._client = _ollama.AsyncClient(host=config.OLLAMA_BASE_URL)
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_cache_max = 256

    @property
    def model_name(self) -> str:
        return self._model

    def _build_messages(
        self, messages: list, system: str | None
    ) -> list[dict]:
        result = []
        if system:
            result.append({"role": "system", "content": system})
        for m in messages:
            if isinstance(m, dict):
                result.append({"role": m["role"], "content": m["content"]})
            else:
                result.append({"role": m.role, "content": m.content})
        return result

    async def unload_others(self) -> None:
        """Entlädt alle anderen Ollama-Modelle um RAM für dieses freizumachen."""
        try:
            running = await self._client.ps()
            for m in (running.models or []):
                name = getattr(m, "name", None) or getattr(m, "model", None) or ""
                if name and not name.startswith(self._model.split(":")[0]):
                    await self._client.chat(
                        model=name,
                        messages=[{"role": "user", "content": ""}],
                        options=config.ollama_options(name, num_predict=1),
                        keep_alive=0,
                    )
                    log.info(f"🗑️  Entlade {name} für {self._model}")
        except Exception as e:
            log.debug(f"unload_others fehlgeschlagen (RAM evtl. nicht freigegeben): {e}")

    async def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        think: bool = False,
        format: str | dict | None = None,
        free_ram: bool = False,
    ) -> str:
        if free_ram:
            await self.unload_others()
        async with GATE:
            response = await self._client.chat(
                model=self._model,
                messages=self._build_messages(messages, system),
                options=config.ollama_options(
                    self._model, temperature=temperature, num_predict=max_tokens
                ),
                keep_alive=config.OLLAMA_KEEP_ALIVE,
                think=think,
                **({"format": format} if format else {}),
            )
        try:
            from core import llm_usage
            llm_usage.record("ollama", self._model,
                             getattr(response, "prompt_eval_count", 0) or 0,
                             getattr(response, "eval_count", 0) or 0,
                             purpose="background")
        except Exception:
            pass
        return response.message.content

    async def stream(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        async for chunk in await self._client.chat(
            model=self._model,
            messages=self._build_messages(messages, system),
            options=config.ollama_options(
                self._model, temperature=temperature, num_predict=max_tokens
            ),
            stream=True,
        ):
            if chunk.message.content:
                yield chunk.message.content

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
        emb = response.embeddings[0]
        if key in self._embed_cache:
            self._embed_cache.move_to_end(key)
        else:
            if len(self._embed_cache) >= self._embed_cache_max:
                self._embed_cache.popitem(last=False)
            self._embed_cache[key] = emb
        return emb

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch-Embedding: mehrere Texte in einem Ollama-Call (spart Round-Trips)."""
        # Prüfe Cache zuerst
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts:   list[str]  = []

        for i, text in enumerate(texts):
            key = _embed_cache_key(text)
            if key in self._embed_cache:
                self._embed_cache.move_to_end(key)
                results[i] = self._embed_cache[key]
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            async with GATE:
                response = await self._client.embed(
                    model=self._embed_model,
                    input=uncached_texts,
                    options=config.ollama_options(self._embed_model),
                )
            for idx, (orig_i, text) in enumerate(zip(uncached_indices, uncached_texts)):
                emb = response.embeddings[idx]
                key = _embed_cache_key(text)
                if len(self._embed_cache) >= self._embed_cache_max:
                    self._embed_cache.popitem(last=False)
                self._embed_cache[key] = emb
                results[orig_i] = emb

        return [r for r in results if r is not None]

    async def check_model(self) -> bool:
        """Prüft ob das Modell verfügbar ist."""
        try:
            models = await self._client.list()
            names = [m.model for m in models.models]
            return any(self._model in n for n in names)
        except Exception:
            return False

    async def pull_if_missing(self) -> None:
        """Lädt Modell herunter falls nicht vorhanden."""
        if not await self.check_model():
            print(f"📥 Lade Modell {self._model}...")
            await self._client.pull(self._model)
            print(f"✅ {self._model} bereit")

        embed_ok = False
        try:
            models = await self._client.list()
            names = [m.model for m in models.models]
            embed_ok = any(self._embed_model in n for n in names)
        except Exception:
            pass

        if not embed_ok:
            print(f"📥 Lade Embedding-Modell {self._embed_model}...")
            await self._client.pull(self._embed_model)
            print(f"✅ {self._embed_model} bereit")
