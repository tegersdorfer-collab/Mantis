"""
Claude API Provider – für schwere Tasks die lokales Modell überfordern.
Nur gezielt einsetzen um API-Kosten zu minimieren.
"""
from typing import AsyncIterator
import anthropic

from .base import LLMProvider, Message
import config


class ClaudeProvider(LLMProvider):
    def __init__(self, model: str | None = None):
        self._model = model or config.CLAUDE_CHAT_MODEL
        self._client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    @property
    def model_name(self) -> str:
        return self._model

    def _to_anthropic(self, messages: list) -> list[dict]:
        """Akzeptiert sowohl Message-Objekte als auch dict-Nachrichten
        ({"role":..., "content":...}) — Caller im Code nutzen beide Formen."""
        out = []
        for m in messages:
            role = m["role"] if isinstance(m, dict) else m.role
            content = m["content"] if isinstance(m, dict) else m.content
            if role in ("user", "assistant"):
                out.append({"role": role, "content": content})
        return out

    async def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        format: str | dict | None = None,
    ) -> str:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=self._to_anthropic(messages),
        )
        if system:
            kwargs["system"] = system
        if format:                       # Claude: JSON-Ausgabe per System-Hinweis erbitten
            sys_extra = "\n\nAntworte ausschließlich mit validem JSON, ohne Text drumherum."
            kwargs["system"] = (kwargs.get("system", "") + sys_extra).strip()
        response = await self._client.messages.create(**kwargs)
        self._record_usage(response)
        return response.content[0].text

    def _record_usage(self, response) -> None:
        try:
            from core import llm_usage
            u = getattr(response, "usage", None)
            if u:
                llm_usage.record("claude", self._model,
                                 getattr(u, "input_tokens", 0) or 0,
                                 getattr(u, "output_tokens", 0) or 0,
                                 purpose="background")
        except Exception:
            pass

    async def stream(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=self._to_anthropic(messages),
        )
        if system:
            kwargs["system"] = system
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
            try:
                self._record_usage(await stream.get_final_message())
            except Exception:
                pass

    async def embed(self, text: str) -> list[float]:
        # Claude hat kein Embedding-API → Fehler werfen damit Caller
        # auf OllamaEmbeddingProvider für Embeddings zurückfällt
        raise NotImplementedError(
            "Claude hat kein Embedding-API. Nutze OllamaEmbeddingProvider.embed()."
        )
