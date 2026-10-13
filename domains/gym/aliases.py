"""Übungs-Aliasse und sichere, deterministische Auflösung."""

from __future__ import annotations

import difflib
import re
import unicodedata

from core import db


BUILTIN_ALIAS_GROUPS = {
    "Bankdrücken": ("Bankdrücken", "Bank", "BD", "Bench", "Bench Press"),
    "Kniebeuge": ("Kniebeuge", "Kniebeugen", "Squat", "Squats"),
    "Beinpresse": ("Beinpresse", "Leg Press"),
    "Schulterdrücken": (
        "Schulterdrücken", "Shoulder Press", "Overhead Press", "OHP",
    ),
    "Klimmzüge": ("Klimmzüge", "Klimmzug", "Pull-up", "Pull-ups", "Pullups"),
    "Bizepscurls": ("Bizepscurls", "Bizepscurl", "Biceps Curl", "Bicep Curl"),
}


def normalize_alias(value: str) -> str:
    """Normalisiert Schreibweise, Umlaute, Bindestriche und Leerzeichen."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = value.replace("&shy;", "").replace("\u00ad", "")
    value = re.sub(r"[-‐‑‒–—]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def is_pullup_name(value: str | None) -> bool:
    compact = normalize_alias(value or "").replace(" ", "")
    return compact in {"klimmzug", "klimmzuege", "pullup", "pullups", "chinup", "chinups"}


def builtin_aliases() -> dict[str, list[str]]:
    """Liefert normalisierte Aliasse, ohne DB-Zugriff."""
    result: dict[str, list[str]] = {}
    for canonical, names in BUILTIN_ALIAS_GROUPS.items():
        for name in names:
            result.setdefault(normalize_alias(name), []).append(canonical)
    return result


def load_aliases() -> dict[str, list[str]]:
    """Kombiniert feste Chat-Aliasse mit gespeicherten Aliasen und Übungsnamen."""
    result = builtin_aliases()
    try:
        alias_rows = db.query(
            """SELECT ea.alias, e.name AS exercise FROM exercise_aliases ea
               JOIN exercises e ON e.id=ea.exercise_id
               WHERE ea.exercise_id IS NOT NULL"""
        )
        exercise_rows = db.query("SELECT name AS alias, name AS exercise FROM exercises")
    except Exception:
        return result
    for row in alias_rows + exercise_rows:
        alias = normalize_alias(row.get("alias"))
        exercise = (row.get("exercise") or "").strip()
        if alias and exercise:
            # Ein gespeicherter Alias ist an eine konkrete Bestands-ID gebunden.
            # Diese Zuordnung hat Vorrang vor dem vorläufigen Built-in-Namen.
            result[alias] = [exercise]
    existing = list(dict.fromkeys(
        (row.get("exercise") or "").strip() for row in alias_rows + exercise_rows
        if row.get("exercise")
    ))
    for canonical_aliases in BUILTIN_ALIAS_GROUPS.values():
        keys = {normalize_alias(name) for name in canonical_aliases}
        matches = [name for name in existing if normalize_alias(name) in keys]
        if len(matches) == 1:
            for key in keys:
                result[key] = matches
        elif len(matches) > 1:
            for name in canonical_aliases:
                key = normalize_alias(name)
                exact = [candidate for candidate in matches if normalize_alias(candidate) == key]
                result[key] = exact or matches
    return result


def resolve_alias(
    value: str,
    aliases: dict[str, list[str]] | None = None,
    *,
    allow_typo: bool = True,
) -> tuple[str | None, tuple[str, ...], bool]:
    """Gibt (kanonischer Name, Mehrdeutigkeiten, Tippfehlerkorrektur) zurück."""
    key = normalize_alias(value)
    mapping = aliases if aliases is not None else builtin_aliases()
    choices = list(dict.fromkeys(mapping.get(key, [])))
    if len(choices) == 1:
        return choices[0], (), False
    if len(choices) > 1:
        return None, tuple(sorted(choices)), False
    if not allow_typo or not key:
        return None, (), False

    candidates = []
    for alias, exercises in mapping.items():
        if abs(len(alias) - len(key)) > max(2, int(len(key) * 0.2)):
            continue
        score = difflib.SequenceMatcher(a=key, b=alias).ratio()
        for exercise in exercises:
            candidates.append((score, exercise, alias))
    candidates.sort(reverse=True)
    if not candidates or candidates[0][0] < 0.84:
        return None, (), False
    best = candidates[0]
    other_exercises = {exercise for score, exercise, _alias in candidates[1:] if score >= best[0] - 0.04}
    if other_exercises - {best[1]}:
        return None, tuple(sorted(other_exercises | {best[1]})), False
    return best[1], (), True

