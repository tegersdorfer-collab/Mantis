"""Vorarbeit für den WebSocket-Voice-Turn, die vor dem finalen Text sicher ist."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.voice_text import partial_text_matches_final

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceTurnPrework:
    text: str
    system: str
    keyword_tools: tuple[str, ...]
    force_tools: bool
    elapsed_ms: float


class VoicePreworkCache:
    """Hält vorbereitete Ergebnisse nach ihrem Teiltext getrennt.

    Es läuft höchstens ein Kontextaufbau gleichzeitig. Währenddessen wird nur
    der jüngste neue Teiltext vorgemerkt. Ein Ergebnis wird nur für den zuletzt
    gehörten Teiltext und einen dazu passenden finalen Text herausgegeben.
    """

    def __init__(self, prepare: Callable[[str], Awaitable[VoiceTurnPrework] | VoiceTurnPrework]):
        self._prepare = prepare
        self.latest_text = ""
        self._cache: OrderedDict[str, VoiceTurnPrework] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._pending: tuple[int, str] | None = None
        self._generation = 0
        self._closed = False

    def offer(self, text: str) -> None:
        if self._closed:
            return
        value = str(text or "").strip()
        if not value:
            return
        self.latest_text = value
        if self._task is not None and not self._task.done():
            self._pending = (self._generation, value)
            return
        self._start(value, self._generation)

    def take_for(self, final_text: str) -> VoiceTurnPrework | None:
        if (
            self._closed
            or not self.latest_text
            or not partial_text_matches_final(self.latest_text, final_text)
        ):
            return None
        return self._cache.pop(self.latest_text, None)

    def clear(self) -> None:
        self._generation += 1
        self.latest_text = ""
        self._cache.clear()
        self._pending = None

    async def close(self) -> None:
        """Schließt den Cache der WebSocket-Session und bricht Vorarbeit ab."""
        if self._closed:
            return
        self._closed = True
        self.clear()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    def _start(self, text: str, generation: int) -> None:
        self._task = asyncio.create_task(self._build(text, generation))

    async def _build(self, text: str, generation: int) -> None:
        current_task = asyncio.current_task()
        try:
            result = self._prepare(text)
            if inspect.isawaitable(result):
                result = await result
            if generation == self._generation:
                self._cache[text] = result
                self._cache.move_to_end(text)
                while len(self._cache) > 8:
                    self._cache.popitem(last=False)
        except Exception as exc:
            log.debug("Voice-Vorarbeit fehlgeschlagen (%s)", type(exc).__name__)
        finally:
            if self._task is current_task:
                self._task = None
            pending = self._pending
            self._pending = None
            if pending is not None:
                pending_generation, pending_text = pending
                if (
                    pending_generation == self._generation
                    and pending_text != text
                ):
                    self._start(pending_text, pending_generation)
