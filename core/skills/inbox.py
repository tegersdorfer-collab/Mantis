"""Agent-Werkzeuge für Suche und Aktionen in der Merk-Inbox."""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from core import db, tools as T
from core.skill_context import CTX
from domains.inbox import store
from domains.inbox.query import SearchQuery, parse_natural_query

_BERLIN = ZoneInfo("Europe/Berlin")
_CATEGORIES = ["anschauen", "lesen", "film-serie", "kaufen", "idee", "ort", "zitat", "sonstiges"]
_LABELS = {
    "anschauen": "Anschauen", "lesen": "Lesen", "film-serie": "Film",
    "kaufen": "Kaufen", "idee": "Idee", "ort": "Ort", "zitat": "Zitat",
    "sonstiges": "Sonstiges",
}


@T.register(
    "inbox_search",
    "Sucht ausschließlich in Timos Merk-Inbox. Verwende dieses Tool für Merker, die er später "
    "wiederfinden will. Übersetze die Frage in query, category, Zeitraum, status und has_url; "
    "lasse ungenannte Filter leer. Status ist standardmäßig open.",
    {
        "query": {"type": "string", "description": "Thema oder Suchbegriffe; leer bei reinen Filtern"},
        "category": {"type": "string", "enum": _CATEGORIES},
        "created_after": {"type": "string", "description": "ISO-Datum/Zeit inklusive Zeitzone"},
        "created_before": {"type": "string", "description": "ISO-Datum/Zeit exklusiv"},
        "status": {"type": "string", "enum": ["open", "done", "all"]},
        "has_url": {"type": "boolean"},
    },
    [],
    "inbox",
)
async def _inbox_search(
    query: str = "",
    category: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    status: str | None = None,
    has_url: bool | None = None,
):
    started = time.monotonic()
    query = (query or "").strip()
    inferred = parse_natural_query(query)
    if inferred.is_mutation:
        return "Das ist eine Änderung, keine Suche. Ich frage nach, bevor ich etwas ändere."
    query_text = inferred.query if any((
        inferred.category, inferred.created_after, inferred.created_before,
        inferred.has_url, inferred.status is None, inferred.status != "open",
    )) else query
    category = category or inferred.category
    created_after_value = _datetime(created_after) if created_after else inferred.created_after
    created_before_value = _datetime(created_before) if created_before else inferred.created_before
    requested_status = status if status is not None else inferred.status
    spec = SearchQuery(
        query=query_text,
        category=category if category in _CATEGORIES else None,
        created_after=created_after_value,
        created_before=created_before_value,
        status=(
            None if requested_status == "all"
            else requested_status if requested_status in {"open", "done"}
            else "open"
        ),
        has_url=has_url if has_url is not None else inferred.has_url,
    )
    embedding = None
    if query and CTX.llm and hasattr(CTX.llm, "embed"):
        try:
            embedding = await asyncio.wait_for(CTX.llm.embed(query), timeout=3.0)
        except Exception:
            embedding = None
    try:
        rows = store.search_items(spec, embedding=embedding, limit=8)
    except Exception:
        _metric("search", "error", count=0, duration_ms=_elapsed_ms(started))
        raise
    response = _format_results(rows)
    _metric("search", "found" if rows else "empty", count=len(rows), duration_ms=_elapsed_ms(started))
    return response


@T.register(
    "inbox_complete",
    "Markiert einen eindeutig gemeinten Inbox-Merker als erledigt. Nutze item_id oder reference, "
    "wenn genannt. Ohne Bezug darf nur der einzige offene Merker gewählt werden; bei mehreren "
    "immer nach ID/Titel fragen.",
    {
        "item_id": {"type": "integer"},
        "reference": {"type": "string", "description": "ID oder Discord-Quellnachrichten-ID"},
        "query": {"type": "string", "description": "Eindeutiger Titel oder Thema"},
    }, [], "inbox",
)
def _inbox_complete(item_id: int | None = None, reference: str | None = None, query: str | None = None):
    started = time.monotonic()
    try:
        result = store.mark_done(
            item_id=item_id, reference=reference, query=query,
            allow_recent=not item_id and not reference and not query,
        )
    except Exception:
        _metric("complete", "error", duration_ms=_elapsed_ms(started))
        raise
    status = result["status"]
    _metric("complete", status, duration_ms=_elapsed_ms(started))
    if status == "done":
        return f"✅ Als erledigt markiert: {result['item']['title']} · ID {result['item']['id']}"
    if status == "already_done":
        return f"Schon erledigt: {result['item']['title']} · ID {result['item']['id']}"
    if status == "ambiguous":
        return "Welchen Merker meinst du? Nenne den Titel oder die ID."
    return "Ich finde keinen passenden offenen Merker."


@T.register(
    "inbox_correct",
    "Korrigiert nur ausdrücklich genannte Felder eines eindeutig gefundenen Inbox-Merkers. "
    "Bei mehreren Treffern zuerst nachfragen. Erlaubte Felder: title, user_note, category, tags, source_urls.",
    {
        "item_id": {"type": "integer"},
        "reference": {"type": "string"},
        "query": {"type": "string", "description": "Eindeutiger bisheriger Titel/Thema"},
        "title": {"type": "string"},
        "user_note": {"type": "string"},
        "category": {"type": "string", "enum": _CATEGORIES},
        "tags": {"type": "array", "items": {"type": "string"}},
        "source_urls": {"type": "array", "items": {"type": "string"}},
    }, [], "inbox",
)
def _inbox_correct(
    item_id: int | None = None,
    reference: str | None = None,
    query: str | None = None,
    title: str | None = None,
    user_note: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    source_urls: list[str] | None = None,
):
    started = time.monotonic()
    changes = {
        "title": title, "user_note": user_note, "category": category,
        "tags": tags, "source_urls": source_urls,
    }
    changes = {key: value for key, value in changes.items() if value is not None}
    if not changes:
        _metric("correct", "needs_fields", duration_ms=_elapsed_ms(started))
        return "Was soll ich an dem Merker korrigieren?"
    try:
        target = store.resolve_item(item_id=item_id, reference=reference, query=query)
    except Exception:
        _metric("correct", "error", duration_ms=_elapsed_ms(started))
        raise
    if target["status"] == "ambiguous":
        _metric("correct", "ambiguous", duration_ms=_elapsed_ms(started))
        return "Mehrere Merker passen. Nenne die ID oder einen genaueren Titel."
    if target["status"] != "found":
        _metric("correct", "not_found", duration_ms=_elapsed_ms(started))
        return "Ich finde keinen passenden Merker."
    try:
        updated = store.correct_item(target["item"]["id"], **changes)
    except Exception:
        _metric("correct", "error", duration_ms=_elapsed_ms(started))
        raise
    _metric("correct", "updated" if updated else "not_found", duration_ms=_elapsed_ms(started))
    if not updated:
        return "Der Merker wurde inzwischen entfernt."
    return f"✏️ Korrigiert: {updated['title']} · ID {updated['id']}"


@T.register(
    "inbox_delete",
    "Löscht einen eindeutig referenzierten Inbox-Merker endgültig. Niemals Sammellöschung. "
    "Bei mehreren Treffern erst nachfragen.",
    {
        "item_id": {"type": "integer"},
        "reference": {"type": "string"},
        "query": {"type": "string", "description": "Eindeutiger Titel/Thema"},
    }, [], "inbox",
)
def _inbox_delete(item_id: int | None = None, reference: str | None = None, query: str | None = None):
    started = time.monotonic()
    try:
        result = store.delete_item(item_id=item_id, reference=reference, query=query)
    except Exception:
        _metric("delete", "error", duration_ms=_elapsed_ms(started))
        raise
    status = result["status"]
    _metric("delete", status, duration_ms=_elapsed_ms(started))
    if status == "deleted":
        return f"🗑️ Gelöscht: {result['item']['title']} · ID {result['item']['id']}"
    if status == "ambiguous":
        return "Mehrere Merker passen. Welchen soll ich endgültig löschen? Nenne Titel oder ID."
    return "Ich finde keinen eindeutig passenden Merker; nichts wurde gelöscht."


def _datetime(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_BERLIN)
        return parsed.astimezone(_BERLIN)
    except (AttributeError, ValueError):
        return None


def _format_results(rows: list[dict]) -> str:
    if not rows:
        return "Nichts gefunden."
    rendered = []
    for row in rows:
        date_value = row.get("created_at")
        date_text = ""
        if date_value:
            if date_value.tzinfo is None:
                date_value = date_value.replace(tzinfo=_BERLIN)
            date_text = date_value.astimezone(_BERLIN).strftime("%d.%m.%Y")
        category = _LABELS.get(row.get("category"), "Sonstiges")
        status = "erledigt" if row.get("status") == "done" else "offen"
        urls = row.get("source_urls") or []
        source = f" · {urls[0]}" if urls else ""
        rendered.append(
            f"ID {row['id']} · {row['title']} · {category} · {date_text} · {status}{source}"
        )
    return "\n".join(rendered)


def _metric(
    operation: str,
    status: str,
    *,
    count: int | None = None,
    duration_ms: int | None = None,
) -> None:
    detail = {"status": status}
    if count is not None:
        detail["count"] = int(count)
    if duration_ms is not None:
        detail["duration_ms"] = max(0, int(duration_ms))
    db.log_event("inbox", operation, detail)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
