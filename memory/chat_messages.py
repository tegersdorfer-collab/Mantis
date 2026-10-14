"""Gemeinsame Vertrauensfilter für gespeicherte Gesprächsnachrichten."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


AGENT_REMOTE_REPLY_PREFIX = "Agent-Remote"
PROMPT_SAFE_SQL = """
COALESCE(lower(meta->>'untrusted'), 'false') <> 'true'
AND COALESCE(meta->>'use_case', '') <> 'agent'
AND NOT (role = 'assistant' AND ltrim(content) LIKE 'Agent-Remote%')
"""


def _message_meta(message: Mapping[str, Any]) -> dict[str, Any]:
    meta = message.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            return {}
    return dict(meta) if isinstance(meta, Mapping) else {}


def is_safe_for_prompt(message: Mapping[str, Any]) -> bool:
    """Schließt nicht vertrauenswürdige und Agent-Remote-Nachrichten aus."""
    meta = _message_meta(message)
    if meta.get("untrusted") is True or str(meta.get("untrusted", "")).casefold() == "true":
        return False
    if meta.get("use_case") == "agent":
        return False
    return not (
        message.get("role") == "assistant"
        and str(message.get("content", "")).lstrip().startswith(AGENT_REMOTE_REPLY_PREFIX)
    )


def filter_prompt_messages(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Gibt nur Nachrichten zurück, die in einen LLM-Prompt gelangen dürfen."""
    return [message for message in messages if is_safe_for_prompt(message)]


def load_prompt_chat_messages(
    *,
    limit: int = 20,
    role: str | None = None,
    after: datetime | None = None,
) -> list[dict[str, Any]]:
    """Lädt und filtert Chat-History, bevor ein Prompt-Konsument sie verarbeitet."""
    from core import db

    conditions = [PROMPT_SAFE_SQL]
    params: list[Any] = []
    if role is not None:
        conditions.append("role = %s")
        params.append(role)
    if after is not None:
        conditions.append("created_at > %s")
        params.append(after)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(max(1, int(limit)))
    rows = db.query(
        "SELECT role, content, channel, created_at, meta FROM chat_messages"
        f"{where} ORDER BY created_at DESC LIMIT %s",
        tuple(params),
    )
    return list(filter_prompt_messages(rows))
