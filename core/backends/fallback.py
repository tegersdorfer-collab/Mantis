"""
FallbackBackend — hält den Agenten am Leben wenn das primäre Backend ausfällt.

Primär: Claude API (Haiku). Fällt sie aus (Netz weg, Anthropic-Ausfall,
Rate-Limit, Timeout), übernimmt das lokale Ollama-Backend transparent —
Mantis bleibt offline funktionsfähig ("Lokal first").

Circuit-Breaker light: Nach einem Primär-Fehler gehen Folge-Calls für
COOLDOWN_S direkt ans Fallback, damit ein toter API-Endpunkt nicht jeden
Turn erst um den Timeout verzögert. Danach wird das Primär-Backend wieder
probiert.
"""
import asyncio
import logging
import time

from core.backends.base import AgentBackend, StreamCb

log = logging.getLogger(__name__)

PRIMARY_TIMEOUT_S = 90   # pro Einzel-Call (Streaming inklusive)
COOLDOWN_S        = 180  # nach Primär-Fehler direkt zum Fallback


class FallbackBackend(AgentBackend):
    def __init__(self, primary: AgentBackend, fallback: AgentBackend,
                 timeout_s: float = PRIMARY_TIMEOUT_S,
                 cooldown_s: float = COOLDOWN_S):
        self._primary   = primary
        self._fallback  = fallback
        self._timeout_s = timeout_s
        self._cooldown_s = cooldown_s
        self._primary_down_until = 0.0

    @property
    def model_name(self) -> str:
        if self._primary_down:
            return f"{self._fallback.model_name} (Fallback)"
        return self._primary.model_name

    @property
    def _primary_down(self) -> bool:
        return time.time() < self._primary_down_until

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
        kwargs = dict(
            messages=messages, tools=tools, stream_cb=stream_cb,
            temperature=temperature, max_tokens=max_tokens,
            think=think, force_tool_call=force_tool_call,
        )

        if not self._primary_down:
            try:
                return await asyncio.wait_for(
                    self._primary.call(**kwargs), timeout=self._timeout_s
                )
            except Exception as e:
                self._primary_down_until = time.time() + self._cooldown_s
                detail = str(e).strip()
                failure = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
                log.warning(
                    f"Primär-Backend {self._primary.model_name} ausgefallen "
                    f"({failure}) — Fallback auf "
                    f"{self._fallback.model_name} für {self._cooldown_s:.0f}s"
                )
                self._report_failover(e)

        return await self._fallback.call(**kwargs)

    def _report_failover(self, exc: Exception) -> None:
        """Ausfall sichtbar machen: Fehler-Widget + Status-Feed."""
        try:
            from core.db import log_error
            message = str(exc).strip() or type(exc).__name__
            recorded_exc = exc if str(exc).strip() else RuntimeError(message)
            log_error("agent_fallback", recorded_exc)
        except Exception:
            pass
        try:
            from core.status import BUS
            BUS.emit("thinking", "⚠️ Claude nicht erreichbar — lokales Modell übernimmt",
                     detail=self._fallback.model_name)
        except Exception:
            pass

    async def warmup(self) -> None:
        # Beide aufwärmen: das Fallback muss im Ernstfall sofort bereit sein.
        await self._primary.warmup()
        try:
            await self._fallback.warmup()
        except Exception as e:
            log.debug(f"Fallback-Warmup: {e}")
