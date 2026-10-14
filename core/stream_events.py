"""Strukturierte Events für LLM-Generierungsversuche."""
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

StreamEventKind = Literal[
    "attempt_start", "delta", "attempt_end", "attempt_abort", "tool_start",
]


@dataclass(frozen=True, slots=True)
class StreamEvent:
    kind: StreamEventKind
    attempt_id: str
    text_delta: str = ""
    final: bool | None = None
    tool_name: str = ""


def new_attempt_id() -> str:
    return uuid4().hex
