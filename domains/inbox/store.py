"""PostgreSQL-Zugriff für die strikt getrennte Merk-Inbox."""

from __future__ import annotations

import json
import math

from core import db
from domains.inbox.capture import normalize_tag, normalize_url
from domains.inbox.query import SearchQuery


CATEGORIES = {
    "anschauen", "lesen", "film-serie", "kaufen", "idee", "ort", "zitat", "sonstiges",
}
SOURCE_KINDS = {"link", "image", "text", "mixed"}


def create_item(
    *,
    title: str,
    user_note: str = "",
    source_kind: str = "text",
    source_urls: list[str] | None = None,
    platform: str = "unknown",
    source_metadata: dict | None = None,
    image_description: str = "",
    category: str = "sonstiges",
    tags: list[str] | None = None,
    source_message_id: str | None = None,
    embedding: list[float] | None = None,
) -> dict:
    """Erstellt einen Merker, dedupliziert Gateway-Retrys und offene Links."""
    title = (title or "").strip()[:500] or "Merker"
    if source_kind not in SOURCE_KINDS:
        raise ValueError("Ungültige Merker-Quelle")
    if category not in CATEGORIES:
        category = "sonstiges"

    urls = _unique(source_urls or [])
    normalized_urls = _unique([normalize_url(url) for url in urls if normalize_url(url)])
    message_id = str(source_message_id) if source_message_id is not None else None

    if message_id:
        existing = _find_message_item(message_id)
        if existing:
            return {"item": existing, "created": False, "duplicate_link": False}

    if normalized_urls:
        existing = db.query_one(
            "SELECT * FROM merk_inbox_items WHERE status='open' "
            "AND normalized_urls && %s ORDER BY updated_at DESC LIMIT 1",
            (normalized_urls,),
        )
        if existing:
            updated = _append_duplicate(
                existing, urls, normalized_urls, user_note, source_kind,
                source_message_id=message_id,
            )
            return {"item": updated, "created": False, "duplicate_link": True}

    vector = _vector_literal(embedding)
    dedupe_key = normalized_urls[0] if normalized_urls else None
    row = db.query_one(
        """INSERT INTO merk_inbox_items
            (title, user_note, source_kind, source_urls, normalized_urls, dedupe_key,
             platform, source_metadata, image_description, category, tags, source_message_id,
             source_message_ids, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::vector)
           ON CONFLICT DO NOTHING RETURNING *""",
        (
            title, (user_note or "").strip(), source_kind, urls, normalized_urls, dedupe_key,
            platform or "unknown", json.dumps(source_metadata or {}, ensure_ascii=False),
            (image_description or "")[:12000], category, _unique_tags(tags or []), message_id,
            [message_id] if message_id else [], vector,
        ),
    )
    if row:
        return {"item": row, "created": True, "duplicate_link": False}

    # Ein gleichzeitiger Discord-Retry oder URL-Share kann zwischen Vorabfrage und
    # INSERT gewonnen haben. Die Unique-Indizes bleiben die letzte Idempotenzgrenze.
    if message_id:
        existing = _find_message_item(message_id)
        if existing:
            return {"item": existing, "created": False, "duplicate_link": False}
    if dedupe_key:
        existing = db.query_one(
            "SELECT * FROM merk_inbox_items WHERE status='open' AND dedupe_key=%s",
            (dedupe_key,),
        )
        if existing:
            updated = _append_duplicate(
                existing, urls, normalized_urls, user_note, source_kind,
                source_message_id=message_id,
            )
            return {"item": updated, "created": False, "duplicate_link": True}
    raise RuntimeError("Inbox-Merker konnte nicht angelegt werden")


def _append_duplicate(
    existing: dict,
    urls: list[str],
    normalized_urls: list[str],
    user_note: str,
    source_kind: str,
    *,
    source_message_id: str | None = None,
) -> dict:
    merged_urls = _unique([*(existing.get("source_urls") or []), *urls])
    merged_normalized = _unique([*(existing.get("normalized_urls") or []), *normalized_urls])
    old_note = (existing.get("user_note") or "").strip()
    new_note = (user_note or "").strip()
    if new_note and new_note != old_note and new_note not in old_note.splitlines():
        old_note = f"{old_note}\n{new_note}" if old_note else new_note
    merged_kind = existing.get("source_kind") or source_kind
    if merged_kind != source_kind:
        merged_kind = "mixed"
    row = db.query_one(
        """UPDATE merk_inbox_items
           SET source_urls=%s, normalized_urls=%s, user_note=%s, source_kind=%s,
               source_message_ids=CASE
                 WHEN %s IS NULL OR %s=ANY(source_message_ids) THEN source_message_ids
                 ELSE array_append(source_message_ids, %s)
               END,
               updated_at=NOW()
           WHERE id=%s RETURNING *""",
        (
            merged_urls, merged_normalized, old_note, merged_kind,
            source_message_id, source_message_id, source_message_id, existing["id"],
        ),
    )
    return row or existing


def _find_message_item(message_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM merk_inbox_items WHERE source_message_id=%s "
        "OR %s=ANY(source_message_ids) LIMIT 1",
        (message_id, message_id),
    )


def add_source_message_reference(item_id: int, message_id: str) -> dict | None:
    """Registriert Share und Discord-Bestätigung als Reply-Referenzen."""
    value = str(message_id or "").strip()
    if not value:
        return None
    return db.query_one(
        """UPDATE merk_inbox_items
           SET source_message_ids=CASE
             WHEN %s=ANY(source_message_ids) THEN source_message_ids
             ELSE array_append(source_message_ids, %s)
           END,
           confirmation_message_ids=CASE
             WHEN %s=ANY(confirmation_message_ids) THEN confirmation_message_ids
             ELSE array_append(confirmation_message_ids, %s)
           END
           WHERE id=%s RETURNING *""",
        (value, value, value, value, item_id),
    )


def update_enrichment(
    item_id: int,
    *,
    title: str | None = None,
    title_if_current: str | None = None,
    platform: str | None = None,
    source_metadata: dict | None = None,
    image_description: str | None = None,
    category: str | None = None,
    category_if_current: str | None = None,
    tags: list[str] | None = None,
    tags_if_current: list[str] | None = None,
    embedding: list[float] | None = None,
) -> dict | None:
    """Ergänzt best-effort Metadaten nach dem sofortigen Commit."""
    assignments = []
    params = []
    if title and title.strip():
        clean_title = title.strip()[:500]
        if title_if_current is None:
            assignments.append("title=%s")
            params.append(clean_title)
        else:
            assignments.append("title=CASE WHEN title=%s THEN %s ELSE title END")
            params.extend([title_if_current, clean_title])
    if platform:
        assignments.append("platform=%s")
        params.append(platform[:80])
    if source_metadata is not None:
        assignments.append("source_metadata=%s::jsonb")
        params.append(json.dumps(source_metadata, ensure_ascii=False))
    if image_description is not None:
        assignments.append("image_description=%s")
        params.append(image_description[:12000])
    if category in CATEGORIES and category_if_current in CATEGORIES:
        assignments.append(
            "category=CASE WHEN category=%s AND category_source='auto' "
            "THEN %s ELSE category END"
        )
        params.extend([category_if_current, category])
    if tags is not None and tags_if_current is not None:
        assignments.append("tags=CASE WHEN tags=%s THEN %s ELSE tags END")
        params.extend([_unique_tags(tags_if_current), _unique_tags(tags)])
    vector = _vector_literal(embedding)
    if vector is not None:
        assignments.append("embedding=%s::vector")
        params.append(vector)
    if not assignments:
        return db.query_one("SELECT * FROM merk_inbox_items WHERE id=%s", (item_id,))
    assignments.append("updated_at=NOW()")
    params.append(item_id)
    return db.query_one(
        f"UPDATE merk_inbox_items SET {', '.join(assignments)} WHERE id=%s RETURNING *",
        tuple(params),
    )


def search_items(
    search: SearchQuery,
    *,
    embedding: list[float] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Filtert nur merk_inbox_items, danach Keyword-/optionale Vektorsuche."""
    where = []
    where_params = []
    if search.status in {"open", "done"}:
        where.append("status=%s")
        where_params.append(search.status)
    if search.category in CATEGORIES:
        where.append("category=%s")
        where_params.append(search.category)
    if search.created_after is not None:
        where.append("created_at >= %s")
        where_params.append(search.created_after)
    if search.created_before is not None:
        where.append("created_at < %s")
        where_params.append(search.created_before)
    if search.has_url is True:
        where.append("cardinality(source_urls) > 0")
    elif search.has_url is False:
        where.append("cardinality(source_urls) = 0")

    term = (search.query or "").strip()
    vector = _vector_literal(embedding) if term else None
    columns = (
        "id, title, user_note, source_kind, source_urls, platform, source_metadata, "
        "image_description, category, tags, status, created_at, updated_at, completed_at, "
        "source_message_id"
    )
    keyword_expr = "(title ILIKE %s ESCAPE '!' OR user_note ILIKE %s ESCAPE '!' " \
                   "OR image_description ILIKE %s ESCAPE '!' " \
                   "OR array_to_string(tags, ' ') ILIKE %s ESCAPE '!')"
    select_params = []
    where_query = ""
    order = "created_at DESC"
    if term:
        pattern = f"%{_escape_like(term)}%"
        select_params.extend([pattern] * 4)
        columns += f", CASE WHEN {keyword_expr} THEN 1 ELSE 0 END AS keyword_score"
        if vector:
            columns += ", embedding <=> %s::vector AS vector_distance"
            select_params.append(vector)
            where_query = f"({keyword_expr} OR (embedding IS NOT NULL AND embedding <=> %s::vector < 0.6))"
            order = "keyword_score DESC, embedding <=> %s::vector ASC NULLS LAST, created_at DESC"
            where_params.extend([pattern] * 4)
            where_params.append(vector)
            order_params = [vector]
        else:
            where_query = keyword_expr
            order = "keyword_score DESC, created_at DESC"
            where_params.extend([pattern] * 4)
            order_params = []
    else:
        order_params = []

    sql = f"SELECT {columns} FROM merk_inbox_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if where_query:
        sql += (" AND " if where else " WHERE ") + where_query
    sql += f" ORDER BY {order} LIMIT %s"
    params = [*select_params, *where_params, *order_params, max(1, min(int(limit), 20))]
    return db.query(sql, tuple(params))


def mark_done(
    *,
    item_id: int | None = None,
    reference: str | int | None = None,
    query: str | None = None,
    allow_recent: bool = False,
) -> dict:
    target, status = _resolve_target(
        item_id=item_id, reference=reference, query=query,
        status="open", allow_recent=allow_recent,
    )
    if target is None:
        return {"status": status, "item": None}
    row = db.query_one(
        "UPDATE merk_inbox_items SET status='done', completed_at=NOW(), updated_at=NOW() "
        "WHERE id=%s AND status='open' RETURNING *",
        (target["id"],),
    )
    if row:
        return {"status": "done", "item": row}
    return {"status": "already_done", "item": target}


def correct_item(item_id: int, **fields) -> dict | None:
    """Korrigiert ausschließlich die ausdrücklich übergebenen Felder."""
    allowed = {"title", "user_note", "category", "tags", "source_urls"}
    changes = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not changes:
        return db.query_one("SELECT * FROM merk_inbox_items WHERE id=%s", (item_id,))
    assignments = []
    params = []
    if "title" in changes:
        assignments.append("title=%s")
        params.append(str(changes["title"]).strip()[:500])
    if "user_note" in changes:
        assignments.append("user_note=%s")
        params.append(str(changes["user_note"])[:12000])
    if "category" in changes:
        category = changes["category"] if changes["category"] in CATEGORIES else "sonstiges"
        assignments.extend(["category=%s", "category_source='user'"])
        params.append(category)
    if "tags" in changes:
        assignments.append("tags=%s")
        params.append(_unique_tags(changes["tags"] or []))
    if "source_urls" in changes:
        urls = _unique(changes["source_urls"] or [])
        normalized = _unique([normalize_url(url) for url in urls if normalize_url(url)])
        assignments.extend(["source_urls=%s", "normalized_urls=%s", "dedupe_key=%s"])
        params.extend([urls, normalized, normalized[0] if normalized else None])
    assignments.append("updated_at=NOW()")
    params.append(item_id)
    return db.query_one(
        f"UPDATE merk_inbox_items SET {', '.join(assignments)} WHERE id=%s RETURNING *",
        tuple(params),
    )


def resolve_item(
    *,
    item_id: int | None = None,
    reference: str | int | None = None,
    query: str | None = None,
    status: str | None = None,
    allow_recent: bool = False,
) -> dict:
    item, outcome = _resolve_target(
        item_id=item_id, reference=reference, query=query,
        status=status, allow_recent=allow_recent,
    )
    return {"status": "found" if item else outcome, "item": item}


def resolve_confirmation_reference(reference: str | int | None) -> dict:
    """Löst ausschließlich eine Discord-Antwort auf eine Inbox-Bestätigung auf."""
    if reference is None or not str(reference).strip():
        return {"status": "not_found", "item": None}
    ref = str(reference).strip()
    rows = db.query(
        "SELECT * FROM merk_inbox_items WHERE %s=ANY(confirmation_message_ids) "
        "ORDER BY updated_at DESC LIMIT 2",
        (ref,),
    )
    if len(rows) == 1:
        return {"status": "found", "item": rows[0]}
    return {"status": "ambiguous" if rows else "not_found", "item": None}


def delete_item(
    *,
    item_id: int | None = None,
    reference: str | int | None = None,
    query: str | None = None,
) -> dict:
    target, status = _resolve_target(
        item_id=item_id, reference=reference, query=query,
        status=None, allow_recent=False,
    )
    if target is None:
        return {"status": status, "item": None}
    deleted = db.query_one(
        "DELETE FROM merk_inbox_items WHERE id=%s RETURNING id", (target["id"],)
    )
    return {"status": "deleted" if deleted else "not_found", "item": target if deleted else None}


def _resolve_target(
    *,
    item_id: int | None,
    reference: str | int | None,
    query: str | None,
    status: str | None,
    allow_recent: bool,
) -> tuple[dict | None, str]:
    if item_id is not None:
        row = db.query_one("SELECT * FROM merk_inbox_items WHERE id=%s", (item_id,))
        return (row, "not_found") if row else (None, "not_found")
    if reference is not None:
        ref = str(reference).strip()
        rows = db.query(
            "SELECT * FROM merk_inbox_items WHERE source_message_id=%s "
            "OR %s=ANY(source_message_ids) ORDER BY updated_at DESC LIMIT 2",
            (ref, ref),
        )
        if len(rows) == 1:
            return rows[0], "found"
        return (None, "ambiguous") if rows else (None, "not_found")
    if query and query.strip():
        pattern = f"%{_escape_like(query.strip())}%"
        status_filter = " AND status=%s" if status else ""
        params = [pattern, pattern]
        if status:
            params.append(status)
        rows = db.query(
            "SELECT * FROM merk_inbox_items WHERE (title ILIKE %s ESCAPE '!' "
            "OR user_note ILIKE %s ESCAPE '!')"
            f"{status_filter} ORDER BY updated_at DESC LIMIT 3",
            tuple(params),
        )
        if len(rows) == 1:
            return rows[0], "found"
        return (None, "ambiguous") if rows else (None, "not_found")
    if allow_recent and status:
        rows = db.query(
            "SELECT * FROM merk_inbox_items WHERE status=%s ORDER BY updated_at DESC LIMIT 2",
            (status,),
        )
        if len(rows) == 1:
            return rows[0], "found"
        return (None, "ambiguous") if rows else (None, "not_found")
    return None, "needs_reference"


def _vector_literal(values: list[float] | None) -> str | None:
    if values is None:
        return None
    if len(values) != 1024:
        return None
    vector = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            return None
        vector.append(format(number, ".8g"))
    return "[" + ",".join(vector) + "]"


def _unique(values) -> list:
    result = []
    seen = set()
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _escape_like(value: str) -> str:
    """Escaped LIKE-Musterzeichen; `!` ist explizit als Escape-Zeichen gesetzt."""
    return str(value).replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _unique_tags(tags) -> list[str]:
    return _unique([normalize_tag(tag) for tag in tags if normalize_tag(tag)])[:20]
