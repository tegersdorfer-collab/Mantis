"""Übungs-Aliasse und sichere, deterministische Auflösung."""

from __future__ import annotations

import difflib
import re
import unicodedata

from core import db
from domains.gym.legacy import LEGACY_EXERCISES


BUILTIN_ALIAS_GROUPS = {
    "Bankdrücken": ("Bankdrücken", "Bank", "BD", "Bench", "Bench Press"),
    "Kniebeuge": ("Kniebeuge", "Kniebeugen", "Squat", "Squats"),
    "Beinpresse": ("Beinpresse", "Leg Press"),
    "Schulterdrücken": (
        "Schulterdrücken", "Shoulder Press", "Overhead Press", "OHP",
    ),
    "Klimmzüge": ("Klimmzüge", "Klimmzug", "Pull-up", "Pull-ups", "Pullups"),
    "Bizepscurls": (
        "Bizepscurls", "Bizepscurl", "Biceps Curl", "Bicep Curl", "Curls",
    ),
    "Hammercurls": ("Hammercurls", "Hammercurl", "Hammer Curls", "Hammer Curl"),
}

_EQUIPMENT_PREFIXES = (
    (re.compile(r"^(?:kh|kurzhantel(?:n)?)\b[\s-]*", re.I), "dumbbell"),
    (re.compile(r"^(?:lh|langhantel(?:n)?)\b[\s-]*", re.I), "barbell"),
    (re.compile(r"^(?:sz(?:-?stange)?)\b[\s-]*", re.I), "ez_bar"),
    (re.compile(r"^(?:kabel|maschine|maschinen)\b[\s-]*", re.I), "machine"),
)
_EQUIPMENT_SUFFIXES = (
    (re.compile(r"\s*\((?:kurzhantel|kh)\)$", re.I), "dumbbell"),
    (re.compile(r"\s*\((?:langhantel|lh)\)$", re.I), "barbell"),
    (re.compile(r"\s*\((?:sz(?:-?stange)?)\)$", re.I), "ez_bar"),
    (re.compile(r"\s*\((?:maschine|kabel|cable)\)$", re.I), "machine"),
    (re.compile(r"\s+(?:kh|kurzhantel(?:n)?)$", re.I), "dumbbell"),
    (re.compile(r"\s+(?:lh|langhantel(?:n)?)$", re.I), "barbell"),
    (re.compile(r"\s+(?:sz(?:-?stange)?)$", re.I), "ez_bar"),
    (re.compile(r"\s+(?:kabel|maschine|maschinen)$", re.I), "machine"),
)
_EQUIPMENT_SHORT_NAMES = {
    "dumbbell": "KH",
    "barbell": "LH",
    "ez_bar": "SZ",
    "machine": "Kabel",
}


def split_equipment_variant(value: str) -> tuple[str, str | None]:
    """Trennt ein Gerätepräfix oder einen Gerätezusatz vom Übungsnamen."""
    text = str(value or "").strip()
    for pattern, equipment in _EQUIPMENT_PREFIXES:
        match = pattern.match(text)
        if match and text[match.end():].strip():
            return text[match.end():].strip(), equipment
    for pattern, equipment in _EQUIPMENT_SUFFIXES:
        match = pattern.search(text)
        if match and text[:match.start()].strip():
            base = text[:match.start()].strip()
            if equipment == "machine" and not match.group().lstrip().startswith("("):
                if normalize_alias(base).endswith(" am"):
                    continue
            return base, equipment
    return text, None


def _variant_choice(name: str, equipment: str | None) -> str:
    base, embedded_equipment = split_equipment_variant(name)
    if embedded_equipment:
        return name.strip()
    if equipment:
        return f"{_EQUIPMENT_SHORT_NAMES.get(equipment, equipment)} {base}"
    return name.strip()


def normalize_alias(value: str) -> str:
    """Normalisiert Schreibweise, Umlaute, Bindestriche und Leerzeichen."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = value.replace("&shy;", "").replace("\u00ad", "")
    value = re.sub(r"[-‐‑‒–—]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def is_pullup_name(value: str | None) -> bool:
    normalized = normalize_alias(value or "")
    compact = normalized.replace(" ", "")
    if compact in {"klimmzug", "klimmzuege", "pullup", "pullups", "chinup", "chinups"}:
        return True
    # Bestandsnamen wie "Pull-Ups with Wide Overhand Grip"; "Assisted Pull-Ups"
    # (Gewicht = Unterstützung) gehört bewusst nicht dazu.
    return normalized.startswith(("pull ups with ", "pull up with "))


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
            """SELECT ea.alias, e.name AS exercise, e.id AS exercise_id, e.equipment
               FROM exercise_aliases ea
               JOIN exercises e ON e.id=ea.exercise_id
               WHERE ea.exercise_id IS NOT NULL"""
        )
        exercise_rows = db.query(
            "SELECT id AS exercise_id, name AS alias, name AS exercise, equipment FROM exercises"
        )
    except Exception:
        return result

    records: dict[str, dict] = {}
    for row in exercise_rows:
        name = (row.get("exercise") or row.get("alias") or "").strip()
        if not name:
            continue
        identity = str(row.get("exercise_id") or normalize_alias(name))
        records[identity] = {
            "name": name, "equipment": row.get("equipment"), "aliases": {name},
        }
    for row in alias_rows:
        name = (row.get("exercise") or "").strip()
        alias = (row.get("alias") or "").strip()
        if not name or not alias:
            continue
        identity = str(row.get("exercise_id") or normalize_alias(name))
        record = records.setdefault(identity, {
            "name": name, "equipment": row.get("equipment"), "aliases": {name},
        })
        record["aliases"].add(alias)

    groups: dict[str, list[dict]] = {}
    for record in records.values():
        base, embedded_equipment = split_equipment_variant(record["name"])
        record["equipment"] = record.get("equipment") or embedded_equipment
        groups.setdefault(normalize_alias(base), []).append(record)

    variants_by_base: dict[str, list[str]] = {}
    plain_names = []
    for base_key, members in groups.items():
        has_variants = any(member["equipment"] for member in members)
        for record in members:
            name = record["name"]
            # Der Gerätepräfix dient nur der Unterscheidung mehrerer Varianten;
            # eine einzelne Übung behält ihren echten Namen.
            choice = _variant_choice(name, record["equipment"]) if len(members) > 1 else name
            if has_variants:
                variants_by_base.setdefault(base_key, []).append(choice)
            else:
                plain_names.append(name)
            for alias in record["aliases"]:
                key = normalize_alias(alias)
                if key:
                    result.setdefault(key, [])
                    if choice not in result[key]:
                        result[key].append(choice)
            full_name = normalize_alias(name)
            if full_name:
                result.setdefault(full_name, [])
                if choice not in result[full_name]:
                    result[full_name].append(choice)

    for canonical_aliases in BUILTIN_ALIAS_GROUPS.values():
        keys = {normalize_alias(name) for name in canonical_aliases}
        matches = [name for name in plain_names if normalize_alias(name) in keys]
        variant_choices = list(dict.fromkeys(
            choice for base, choices in variants_by_base.items() if base in keys
            for choice in choices
        ))
        if variant_choices:
            for key in keys:
                result[key] = variant_choices.copy()
        elif len(matches) == 1:
            for key in keys:
                result[key] = matches
        elif len(matches) > 1:
            for name in canonical_aliases:
                key = normalize_alias(name)
                exact = [candidate for candidate in matches if normalize_alias(candidate) == key]
                result[key] = exact or matches

    for base, choices in variants_by_base.items():
        unique_choices = list(dict.fromkeys(choices))
        result[base] = unique_choices

    # Bestandsübungen aus dem AlphaProgression-Import: ihr Name ist kanonisch,
    # deutsche Namen und Abkürzungen sind Aliasse darauf.
    by_name = {normalize_alias(record["name"]): record["name"] for record in records.values()}
    for entry in LEGACY_EXERCISES:
        stored = by_name.get(normalize_alias(entry.name))
        if stored is None:
            continue
        base_name, _equipment = split_equipment_variant(stored)
        legacy_choices = list(dict.fromkeys(
            variants_by_base.get(normalize_alias(base_name), [stored])
        ))
        for alias in (entry.name, *entry.aliases):
            key = normalize_alias(alias)
            if key:
                existing = result.get(key, [])
                if len(existing) > 1:
                    for choice in legacy_choices:
                        if choice not in existing:
                            existing.append(choice)
                else:
                    result[key] = legacy_choices.copy()
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

