"""Abstrakte Schnittstelle für Agent-Backends.

Jedes Backend implementiert einen Anbieter; der Agent-Loop kennt nur dieses
Interface.
"""
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from core.stream_events import StreamEvent

StreamCb = Callable[[str], Awaitable[None]]
AttemptEventCb = Callable[[StreamEvent], Awaitable[None]]


class AgentBackend(ABC):
    @property
    def supports_vision(self) -> bool:
        """Ob dieses Backend Modell Bilder in seinem nativen Chat-Aufruf annimmt."""
        return False

    @abstractmethod
    async def call(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
        temperature: float,
        max_tokens: int,
        think: bool = False,
        force_tool_call: bool = False,
    ) -> tuple[str, list[dict]]:
        """
        Ruft das LLM einmalig auf.

        Gibt (antwort_text, tool_calls) zurück.
        tool_calls-Format (einheitlich für alle Backends):
            [{"function": {"name": str, "arguments": dict}}]
        """
        ...

    @abstractmethod
    async def warmup(self) -> None:
        """Lädt das Modell in den RAM (Cold-Start vermeiden)."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...
