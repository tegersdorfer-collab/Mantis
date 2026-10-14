"""Sequenzielle Cloud-Fallback-Kette mit Stage-Timeouts und Cooldowns.

Jeder Call versucht die verfügbaren Anbieter der konfigurierten Reihenfolge.
Sind alle Stufen ausgefallen, wirft die Kette eine definierte Exception.
"""
import asyncio
import logging
import time

from core.backends.base import AgentBackend, StreamCb
from core.error_redaction import redact_error_text

log = logging.getLogger(__name__)

PRIMARY_TIMEOUT_S = 90   # pro Einzel-Call (Streaming inklusive)
COOLDOWN_S        = 180  # nach Primär-Fehler direkt zum Fallback


class AllBackendsFailedError(RuntimeError):
    """Alle konfigurierten Anbieter-Stufen sind fehlgeschlagen oder im Cooldown."""

    def __init__(self, failures: list[str] | None = None):
        self.failures = tuple(failures or ())
        detail = f" ({'; '.join(self.failures)})" if self.failures else ""
        super().__init__(f"Alle Backend-Stufen sind fehlgeschlagen{detail}")


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
        if not backends:
            raise ValueError("FallbackBackend braucht mindestens ein Backend")

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
        self._last_used_backend: AgentBackend | None = None

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
    def last_used_backend(self) -> AgentBackend | None:
        """Letzte erfolgreiche Stufe; bei einem neuen Call vorab zurückgesetzt."""
        return self._last_used_backend

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

        self._last_used_backend = None
        failures: list[str] = []
        for index, backend in enumerate(self._backends):
            if time.time() < self._down_until[index]:
                continue
            try:
                result = backend.call(**kwargs)
                timeout = self._stage_timeouts[index]
                if timeout is None:
                    outcome = await result
                else:
                    outcome = await asyncio.wait_for(result, timeout=timeout)
                self._last_used_backend = backend
                return outcome
            except Exception as e:
                permanently_disabled = bool(getattr(e, "disable_backend", False))
                requested_cooldown = getattr(e, "fallback_cooldown_s", None)
                cooldown = self._cooldown_s if requested_cooldown is None else max(0.0, float(requested_cooldown))
                self._down_until[index] = float("inf") if permanently_disabled else time.time() + cooldown
                if index == 0:
                    self._primary_down_until = self._down_until[index]
                detail = redact_error_text(e).strip()
                failure = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
                failures.append(f"{backend.model_name}: {failure}")
                if index < len(self._backends) - 1:
                    next_backend = self._next_available_backend(index + 1)
                    fallback_period = "bis zum Neustart" if permanently_disabled else f"für {cooldown:.0f}s"
                    log.warning(
                        f"Backend {backend.model_name} ausgefallen ({failure}) — "
                        f"Fallback auf {next_backend.model_name} {fallback_period}"
                    )
                    self._report_failover(e, backend, next_backend)
                else:
                    log.warning("Letzte Backend-Stufe %s ausgefallen (%s)", backend.model_name, failure)

        raise AllBackendsFailedError(failures)

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
            detail = redact_error_text(exc).strip()
            message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            recorded_exc = RuntimeError(message)
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, _record_failover_error, recorded_exc)
            future.add_done_callback(_log_failover_task_error)
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
        # Cloud-Backends laden keine lokalen Modelle; warmup bleibt interface-kompatibel.
        await self._backends[0].warmup()
        for backend in self._backends[1:]:
            try:
                await backend.warmup()
            except Exception as e:
                log.debug(
                    "Fallback-Warmup für %s: %s",
                    backend.model_name,
                    redact_error_text(e),
                )


def _record_failover_error(exc: Exception) -> None:
    try:
        from core.db import log_error
        log_error("agent_fallback", exc)
    except Exception as error:
        log.debug("Fallback-Fehler konnte nicht persistiert werden: %s", redact_error_text(error))


def _log_failover_task_error(future: asyncio.Future) -> None:
    try:
        error = future.exception()
    except Exception as exc:
        log.debug("Fallback-Loggingtask fehlgeschlagen: %s", redact_error_text(exc))
        return
    if error:
        log.debug("Fallback-Loggingtask fehlgeschlagen: %s", redact_error_text(error))
