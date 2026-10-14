"""Enges Suchobjekt und deutsche Zeitfilter für Merk-Inbox-Abfragen."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


_BERLIN = ZoneInfo("Europe/Berlin")
_STOP_WORDS = {
    "alle", "alles", "auch", "bitte", "das", "der", "die", "dieser", "diese",
    "dem", "den", "des", "ein", "eine", "einer", "eines", "für", "fuer",
    "habe", "hab", "ich", "im", "in", "mir", "mit", "noch", "von", "was",
    "welche", "welcher", "welches", "zeig", "zeige", "wollte", "war", "letzter",
    "letzte", "letzten", "woche", "diese", "dieser", "gemerkt", "gespeichert",
    "merker", "links", "link", "anschauen", "ansehen", "filme", "film", "serien",
    "serie", "idee", "erledigte", "erledigt", "gesehen", "status", "offene",
    "offen", "youtube",
}
_MUTATION_PATTERNS = (
    re.compile(r"\b(?:lösch|loesch|entfern|korrigier|änder|aender|update|aktualisier)", re.I),
    re.compile(r"\b(?:hab|habe) ich (?:es )?(?:gesehen|erledigt|abgehakt)\b", re.I),
    re.compile(r"\bmarkier\w*\b.{0,24}\b(?:erledigt|gesehen)\b", re.I),
)


@dataclass(frozen=True)
class SearchQuery:
    query: str = ""
    category: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    status: str | None = "open"
    has_url: bool | None = None
    is_mutation: bool = False


def parse_natural_query(text: str, *, now: datetime | None = None) -> SearchQuery:
    """Übersetzt häufige deutsche Inbox-Fragen in DB-Filter ohne fremde Suche."""
    raw = (text or "").strip()
    lower = raw.casefold()
    if any(pattern.search(lower) for pattern in _MUTATION_PATTERNS):
        return SearchQuery(is_mutation=True, status=None)

    category = _category(lower)
    if re.search(r"\b(?:alle|alles)\b", lower):
        status = None
    elif re.search(r"\b(?:erledigt\w*|gesehen|abgehakt|fertig|done)\b", lower):
        status = "done"
    else:
        status = "open"

    has_url = True if re.search(
        r"\b(?:links?|urls?|youtube|tiktok|instagram)\b|https?://|youtu\.be", lower
    ) else None
    after, before = _time_filter(lower, now=now)
    query = _search_terms(raw, category=category, has_url=has_url, status=status)
    return SearchQuery(
        query=query,
        category=category,
        created_after=after,
        created_before=before,
        status=status,
        has_url=has_url,
    )


def is_inbox_request(text: str, *, has_reference: bool = False) -> bool:
    """Erkennt nur klare Rückfragen/Aktionen zur eigenen Merkliste."""
    lower = (text or "").casefold().strip()
    if not lower:
        return False
    if has_reference and re.search(r"\b(?:lösch|loesch|entfern|korrigier|änder|aender|fertig|gesehen|erledigt)\w*\b", lower):
        return True
    if re.search(r"\b(?:merkliste\w*|merker\w*|gemerkt|gespeichert|inbox)\b", lower):
        return True
    if re.search(r"\bwas wollte ich (?:mir )?(?:noch )?(?:anschauen|ansehen|lesen)\b", lower):
        return True
    if re.search(r"\bwas war die idee mit\b", lower):
        return True
    if re.search(r"\b(?:zeig|zeige) mir links?\b.{0,30}\b(?:letzte|letzter|letzten) woche\b", lower):
        return True
    if re.search(r"\b(?:hab|habe) ich (?:es )?(?:gesehen|erledigt|abgehakt)\b", lower):
        return True
    if any(pattern.search(lower) for pattern in _MUTATION_PATTERNS):
        has_inbox_name = bool(re.search(r"\b(?:merker|merkliste|inbox)\b", lower))
        text_without_urls = re.sub(r"https?://[^\s<>\"']+", " ", lower)
        has_item_id = bool(re.search(
            r"(?<!\w)(?:id|eintrag|merker)\s*(?:nr\.?\s*)?#?\s*\d{1,19}\b|"
            r"(?<!\w)#\d{1,19}\b",
            text_without_urls,
        ))
        return has_inbox_name or has_item_id
    if re.search(r"\b(?:my )?(?:saved|remembered) (?:links?|items?)\b", lower):
        return True
    return False


def _category(lower: str) -> str | None:
    if re.search(r"\b(?:anschauen|ansehen|angucken|videos?|clips?|youtube|tiktok|instagram)\b", lower):
        return "anschauen"
    if re.search(r"\b(?:filme?|serien?|staffeln?)\b", lower):
        return "film-serie"
    if re.search(r"\b(?:ideen?|inspirationen?)\b", lower):
        return "idee"
    if re.search(r"\b(?:artikel|blogs?|bücher|buecher|lesen)\b", lower):
        return "lesen"
    if re.search(r"\b(?:kaufen|wunschliste|angebote?)\b", lower):
        return "kaufen"
    if re.search(r"\b(?:zitate?|sprüche|sprueche)\b", lower):
        return "zitat"
    if re.search(r"\b(?:orte?|restaurants?|reiseziele?)\b", lower):
        return "ort"
    return None


def _time_filter(lower: str, *, now: datetime | None) -> tuple[datetime | None, datetime | None]:
    if now is None:
        now = datetime.now(_BERLIN)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_BERLIN)
    else:
        now = now.astimezone(_BERLIN)

    if "letzte woche" in lower or "letzter woche" in lower:
        this_monday = datetime.combine(
            now.date() - timedelta(days=now.weekday()), time.min, tzinfo=_BERLIN
        )
        return this_monday - timedelta(days=7), this_monday
    if "diese woche" in lower:
        this_monday = datetime.combine(
            now.date() - timedelta(days=now.weekday()), time.min, tzinfo=_BERLIN
        )
        return this_monday, this_monday + timedelta(days=7)
    if "letzte 7 tage" in lower or "letzten sieben tage" in lower:
        return now - timedelta(days=7), now
    return None, None


def _search_terms(
    text: str,
    *,
    category: str | None,
    has_url: bool | None,
    status: str | None,
) -> str:
    del has_url, status  # Filterwörter werden unabhängig vom Status unten entfernt.
    tokens = re.findall(r"#[\w-]+|[\wÀ-ž]+", text, flags=re.UNICODE)
    kept = []
    for token in tokens:
        normalized = token.lstrip("#").casefold()
        if normalized in _STOP_WORDS or normalized in {"was", "wollte", "mir"}:
            continue
        if category == "anschauen" and normalized in {"anschauen", "ansehen", "angucken", "video", "videos", "clip", "clips"}:
            continue
        if category == "film-serie" and normalized in {"film", "filme", "serie", "serien", "staffel", "staffeln"}:
            continue
        if normalized in {"youtube", "tiktok", "instagram", "discord", "merker"}:
            continue
        kept.append(token.lstrip("#"))
    return " ".join(kept)
