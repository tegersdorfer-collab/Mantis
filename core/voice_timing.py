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


class TurnTiming:
    def __init__(self) -> None:
        self.tool_select_ms = 0.0
        self.context_ms = 0.0
        self.tool_exec_ms = 0.0
        self.llm_calls: list[dict] = []
        self.pending_tokens: int | None = None
        self.reflex = False

    def snapshot(self) -> dict:
        return {
            "tool_select_ms": round(self.tool_select_ms, 1),
            "context_ms": round(self.context_ms, 1),
            "llm_calls": [dict(c) for c in self.llm_calls],
            "tool_exec_ms": round(self.tool_exec_ms, 1),
            "reflex": self.reflex,
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
