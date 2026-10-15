"""Leichtgewichtige Phasen-Zeiten eines Voice-Turns (für das voice_latency-Event).

Ein veränderliches Objekt in einer ContextVar: asyncio-Tasks erben die Referenz,
darum sehen auch die vom Router gestarteten Tasks dieselben Messwerte. Ohne
aktives Timing (Chat, Telegram, Tests) sind alle Funktionen No-Ops.
"""
import contextvars
import time
from contextlib import contextmanager

_current: contextvars.ContextVar["TurnTiming | None"] = contextvars.ContextVar(
    "voice_turn_timing", default=None,
)
_PARTIAL_SKIP_REASONS = (
    "busy", "budget", "interval", "no_speech", "disabled", "not_wired",
)


def partial_skip_snapshot(reasons: dict | None = None) -> dict[str, int]:
    values = reasons or {}
    result = {}
    for name in _PARTIAL_SKIP_REASONS:
        try:
            result[name] = max(0, int(values.get(name, 0)))
        except (TypeError, ValueError):
            result[name] = 0
    return result


class TurnTiming:
    def __init__(self) -> None:
        self.tool_select_ms = 0.0
        self.context_ms = 0.0
        self.tool_exec_ms = 0.0
        self.llm_calls: list[dict] = []
        self.pending_tokens: int | None = None
        self.reflex = False
        self.partial_runs = 0
        self.partial_skip_reasons = partial_skip_snapshot()
        self.partial_as_final = False
        self.partial_reused = False
        self.saved_ms = 0.0

    def snapshot(self) -> dict:
        return {
            "tool_select_ms": round(self.tool_select_ms, 1),
            "context_ms": round(self.context_ms, 1),
            "llm_calls": [dict(c) for c in self.llm_calls],
            "tool_exec_ms": round(self.tool_exec_ms, 1),
            "reflex": self.reflex,
            "partial_runs": self.partial_runs,
            "partial_skip_reasons": dict(self.partial_skip_reasons),
            "partial_as_final": self.partial_as_final,
            "partial_reused": self.partial_reused,
            "saved_ms": round(self.saved_ms, 1),
        }


def start() -> contextvars.Token:
    timing = TurnTiming()
    return _current.set(timing)


def reset(token: contextvars.Token) -> None:
    """Stellt den Timing-Kontext wieder her, der vor `start()` aktiv war."""
    _current.reset(token)


def current() -> TurnTiming | None:
    return _current.get()


def mark_reflex() -> None:
    """Markiert eine lokal beantwortete Voice-Anfrage im aktuellen Turn."""
    timing = _current.get()
    if timing is not None:
        timing.reflex = True
        timing.partial_reused = False
        timing.saved_ms = 0.0


def mark_partial_reused(saved_ms: float) -> None:
    """Vermerkt Vorarbeit erst am tatsächlichen Verbrauchsort."""
    timing = _current.get()
    if timing is not None and not timing.reflex:
        timing.partial_reused = True
        timing.saved_ms = max(0.0, float(saved_ms))


def set_partial_skip_reasons(reasons: dict | None) -> None:
    timing = _current.get()
    if timing is not None:
        timing.partial_skip_reasons = partial_skip_snapshot(reasons)


def snapshot() -> dict:
    timing = _current.get()
    return (timing or TurnTiming()).snapshot()


@contextmanager
def phase(name: str):
    """Addiert die Dauer des Blocks (ms) auf das Feld `name` (z. B. 'context_ms')."""
    timing = _current.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            setattr(timing, name, getattr(timing, name) + (time.perf_counter() - started) * 1000)


class LlmCall:
    """Misst einen LLM-Call; mark_first_token() beim ersten Text-Delta."""

    def __init__(self) -> None:
        self._timing = _current.get()
        self._started = time.perf_counter()
        self._first: float | None = None
        if self._timing is not None:
            self._timing.pending_tokens = None

    def mark_first_token(self) -> None:
        if self._first is None:
            self._first = time.perf_counter()

    def finish(self, tool_calls, *, error: str | None = None) -> None:
        if self._timing is None:
            return
        end = time.perf_counter()
        first = self._first
        call = {
            "ms_to_first_token": round(((first or end) - self._started) * 1000, 1),
            "ms_total": round((end - self._started) * 1000, 1),
            "output_tokens": self._timing.pending_tokens,
            "had_tool_calls": bool(tool_calls),
        }
        if error is not None:
            call["error"] = error
        self._timing.llm_calls.append(call)


def note_output_tokens(n: int | None) -> None:
    timing = _current.get()
    if timing is not None:
        timing.pending_tokens = n
