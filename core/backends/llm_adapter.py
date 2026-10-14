"""Adapter, der ein Agent-Backend als einfachen LLM-Provider nutzbar macht."""
from typing import AsyncIterator

from core.backends.base import AgentBackend
from llm.base import LLMProvider, Message


class BackendLLMAdapter(LLMProvider):
    """Leitet Chat-Aufgaben ohne Tools durch dieselbe Backend-Fallback-Kette."""

    def __init__(self, backend: AgentBackend):
        self.backend = backend

    @property
    def model_name(self) -> str:
        return self.backend.model_name

    def _messages(self, messages: list[Message], system: str | None, format) -> list[dict]:
        result = []
        system_text = system or ""
        if format == "json":
            system_text = "\n\n".join(filter(None, (
                system_text,
                "Antworte ausschließlich mit validem JSON, ohne Text drumherum.",
            )))
        elif isinstance(format, dict):
            system_text = "\n\n".join(filter(None, (
                system_text,
                "Antworte als JSON gemäß diesem Schema: " + str(format),
            )))
        if system_text:
            result.append({"role": "system", "content": system_text})
        for message in messages:
            if isinstance(message, dict):
                result.append({"role": message["role"], "content": message["content"]})
            else:
                result.append({"role": message.role, "content": message.content})
        return result

    async def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        format: str | dict | None = None,
        timeout_s: float | None = None,
    ) -> str:
        call_kwargs = dict(
            messages=self._messages(messages, system, format),
            tools=None,
            stream_cb=None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        call_with_timeout = getattr(self.backend, "call_with_timeout", None)
        if timeout_s is not None and callable(call_with_timeout):
            text, _ = await call_with_timeout(**call_kwargs, timeout_s=timeout_s)
        else:
            text, _ = await self.backend.call(**call_kwargs)
        return text

    async def stream(
        self,
        messages: list[Message],
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        parts: list[str] = []

        async def on_progress(text_delta: str) -> None:
            parts.append(text_delta)

        text, _ = await self.backend.call(
            messages=self._messages(messages, system, None),
            tools=None,
            stream_cb=on_progress,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        yield text or "".join(parts)

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("Agent-Backends unterstützen keine Embeddings")
