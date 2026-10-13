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
    def __init__(
        self,
        primary: AgentBackend | None = None,
        fallback: AgentBackend | None = None,
        timeout_s: float = PRIMARY_TIMEOUT_S,
        cooldown_s: float = COOLDOWN_S,
        *,
        backends: list[AgentBackend] | None = None,
        stage_timeouts: list[float | None] | None = None,
    ):
        if backends is None:
            if primary is None or fallback is None:
                raise ValueError("FallbackBackend braucht primary und fallback oder backends")
            backends = [primary, fallback]
        elif primary is not None or fallback is not None:
            raise ValueError("primary/fallback und backends können nicht gemeinsam gesetzt werden")
        if len(backends) < 2:
            raise ValueError("FallbackBackend braucht mindestens zwei Backends")

        if stage_timeouts is None:
            stage_timeouts = [timeout_s] * (len(backends) - 1) + [None]
        if len(stage_timeouts) != len(backends):
            raise ValueError("stage_timeouts muss je Backend genau einen Eintrag haben")

        self._backends = tuple(backends)
        self._stage_timeouts = tuple(stage_timeouts)
        self._cooldown_s = cooldown_s
        self._down_until = [0.0] * len(self._backends)
        # Aliase für bestehende Integrationen, die noch das Zweiermodell erwarten.
        self._primary = self._backends[0]
        self._fallback = self._backends[-1]
        self._timeout_s = self._stage_timeouts[0]
        self._primary_down_until = 0.0

    @property
    def backends(self) -> tuple[AgentBackend, ...]:
        return self._backends

    @property
    def backend_chain(self) -> tuple[str, ...]:
        return tuple(backend.model_name for backend in self._backends)

    @property
    def stage_timeouts(self) -> tuple[float | None, ...]:
        return self._stage_timeouts

    @property
    def model_name(self) -> str:
        now = time.time()
        active = next(
            (i for i in range(len(self._backends) - 1) if now >= self._down_until[i]),
            len(self._backends) - 1,
        )
        name = self._backends[active].model_name
        return f"{name} (Fallback)" if active else name

    @property
    def _primary_down(self) -> bool:
        return time.time() < self._down_until[0]

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

        for index, backend in enumerate(self._backends[:-1]):
            if time.time() < self._down_until[index]:
                continue
            try:
                result = backend.call(**kwargs)
                timeout = self._stage_timeouts[index]
                if timeout is None:
                    return await result
                return await asyncio.wait_for(result, timeout=timeout)
            except Exception as e:
                self._down_until[index] = time.time() + self._cooldown_s
                if index == 0:
                    self._primary_down_until = self._down_until[index]
                detail = str(e).strip()
                failure = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
                next_backend = self._next_available_backend(index + 1)
                log.warning(
                    f"Backend {backend.model_name} ausgefallen ({failure}) — "
                    f"Fallback auf {next_backend.model_name} für {self._cooldown_s:.0f}s"
                )
                self._report_failover(e, backend, next_backend)

        return await self._backends[-1].call(**kwargs)

    def _next_available_backend(self, start: int) -> AgentBackend:
        now = time.time()
        for index in range(start, len(self._backends) - 1):
            if now >= self._down_until[index]:
                return self._backends[index]
        return self._backends[-1]

    def _report_failover(
        self,
        exc: Exception,
        backend: AgentBackend,
        fallback: AgentBackend,
    ) -> None:
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
            BUS.emit(
                "thinking",
                f"⚠️ {backend.model_name} nicht erreichbar — {fallback.model_name} übernimmt",
                detail=fallback.model_name,
            )
        except Exception:
            pass

    async def warmup(self) -> None:
        # Alle Stufen aufwärmen: lokale Fallbacks müssen im Ernstfall bereit sein.
        await self._backends[0].warmup()
        for backend in self._backends[1:]:
            try:
                await backend.warmup()
            except Exception as e:
                log.debug(f"Fallback-Warmup für {backend.model_name}: {e}")
