"""Schneller, enger Parser für Krafttrainings-Sätze im Chat."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from domains.gym.aliases import builtin_aliases, is_pullup_name, normalize_alias, resolve_alias


_NUMBER = re.compile(r"(?<![\w.])[+]?\d+(?:[.,]\d+)?(?![\d.])")
_EQUIPMENT = (
    (re.compile(r"^(?:kh|kurzhantel(?:n)?)\b[\s-]*", re.I), "dumbbell"),
    (re.compile(r"^(?:lh|langhantel(?:n)?)\b[\s-]*", re.I), "barbell"),
    (re.compile(r"^(?:sz(?:-?stange)?)\b[\s-]*", re.I), "ez_bar"),
)
_REJECTION_TERMS = (
    "pound", " lb", "umrechnen", "wie viel", "wieviel", "wie schwer",
    "gelaufen", "laufen", "joggen", "laufband", "ich bin", "ich wiege",
    "kalorie", "kcal",
)
_REP_WORDS = re.compile(
    r"\b(?:kg|kilo|wdh\.?|wiederholungen?|reps?|saetze?|sätze?|mal|à)\b", re.I
)
_NON_EXERCISE_START = re.compile(
    r"^(?:heute|morgen|gestern|ich|wir|du|ihr|er|sie|es|mein(?:e|er|en|em|es)?|"
    r"unser(?:e|er|en|em|es)?|die|der|das|ein(?:e|er|en|em|es)?)\b"
)


@dataclass(frozen=True)
class ParsedSet:
    weight_kg: float | None
    reps: int


@dataclass(frozen=True)
class ParsedLog:
    exercise: str | None
    sets: tuple[ParsedSet, ...]
    equipment: str | None = None
    load_mode: str = "external"
    known: bool = False
    alias: str | None = None
    fuzzy: bool = False
    ambiguous: tuple[str, ...] = ()


def has_non_exercise_start(text: str) -> bool:
    """Erkennt häufige Satzanfänge, die Zahlen zu Smalltalk statt Logs machen."""
    first_number = _NUMBER.search(text or "")
    if not first_number:
        return False
    prefix = normalize_alias(text[:first_number.start()])
    return bool(_NON_EXERCISE_START.match(prefix))


def _numeric_value(raw: str) -> float | None:
    value = raw.strip().lstrip("+").replace(",", ".")
    # Im deutschsprachigen Gym-Kontext ist 1.000 eine übliche Tausendergruppe.
    if re.fullmatch(r"\d{1,3}\.\d{3}", value):
        value = value.replace(".", "")
    try:
        number = float(value)
    except ValueError:
        return None
    return number


def _prefix_and_remainder(text: str, aliases: dict[str, list[str]]):
    equipment = None
    remainder = text.strip()
    for pattern, kind in _EQUIPMENT:
        match = pattern.match(remainder)
        if match:
            equipment = kind
            remainder = remainder[match.end():].strip()
            break

    normalized = normalize_alias(remainder)
    exact = [
        alias for alias in aliases
        if normalized == alias or normalized.startswith(alias + " ")
    ]
    if exact:
        alias_key = max(exact, key=len)
        # Retain the user's spelling for future alias creation/confirmation.
        words = remainder.split()
        boundary = next(
            (index for index in range(1, len(words) + 1)
             if normalize_alias(" ".join(words[:index])) == alias_key),
            len(alias_key.split()),
        )
        original_alias = " ".join(words[:boundary])
        rest = " ".join(words[boundary:])
        exercise, ambiguous, fuzzy = resolve_alias(original_alias, aliases, allow_typo=False)
        return original_alias, rest, equipment, exercise, ambiguous, fuzzy, bool(exercise)

    first_number = _NUMBER.search(remainder)
    if not first_number:
        return remainder, "", equipment, None, (), False, False
    raw_name = remainder[:first_number.start()].strip(" ,:-")
    tail = remainder[first_number.start():].strip()
    if not raw_name:
        return "", tail, equipment, None, (), False, False
    exercise, ambiguous, fuzzy = resolve_alias(raw_name, aliases)
    return raw_name, tail, equipment, exercise, ambiguous, fuzzy, bool(exercise)


def _parse_chunk(chunk: str, allow_bodyweight: bool) -> list[ParsedSet] | None:
    chunk = chunk.strip().replace("×", "x")
    if not chunk:
        return None
    chunk = re.sub(r"(?<=\d)\s*(?:kg|kilo)\b", " ", chunk, flags=re.I)
    if re.search(r"\d+\s*mal\b", chunk, re.I) and not re.search(
        r"\b(?:wdh|wiederholungen?|reps?|saetze?|sätze?)\b", chunk, re.I
    ):
        return None

    # Setzahl x Wiederholungen; ein führendes Lastgewicht bleibt stehen.
    set_rep = re.search(r"(?<![\w.])(\d{1,2})\s*x\s*(\d{1,2})(?![\w.])", chunk, re.I)
    if set_rep:
        count, reps = int(set_rep.group(1)), int(set_rep.group(2))
        if 1 <= count <= 10 and 1 <= reps <= 30:
            before, after = chunk[:set_rep.start()], chunk[set_rep.end():]
            remaining = _REP_WORDS.sub(" ", before + " " + after)
            remaining = re.sub(r"\b(?:x|mal|à)\b", " ", remaining, flags=re.I)
            values = [_numeric_value(m.group()) for m in _NUMBER.finditer(remaining)]
            if any(value is None for value in values) or len(values) > 1:
                return None
            weight = values[0] if values else None
            if weight is not None and weight <= 0:
                return None
            return [ParsedSet(weight, reps) for _ in range(count)]
        if reps < 1 or reps > 30:
            return None

    # Einzelner Satz mit x als Wiederholungsmarker (z.B. 80 kg x 8).
    single_rep = re.search(r"\bx\s*(\d{1,2})\b", chunk, re.I)
    if single_rep:
        reps = int(single_rep.group(1))
        if not 1 <= reps <= 30:
            return None
        remaining = chunk[:single_rep.start()] + " " + chunk[single_rep.end():]
        remaining = _REP_WORDS.sub(" ", remaining)
        values = [_numeric_value(m.group()) for m in _NUMBER.finditer(remaining)]
        if len(values) > 1 or any(v is None or v <= 0 for v in values):
            return None
        return [ParsedSet(values[0] if values else None, reps)]

    # Explizite Schreibweise wie "3 Sätze à 8 Wiederholungen".
    set_words = re.search(
        r"(?<!\w)(\d{1,2})\s*(?:saetze?|sätze?)\s*(?:à|x)?\s*(\d{1,2})\b",
        chunk, re.I,
    )
    if set_words:
        count, reps = int(set_words.group(1)), int(set_words.group(2))
        if not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        remaining = chunk[:set_words.start()] + " " + chunk[set_words.end():]
        remaining = _REP_WORDS.sub(" ", remaining)
        values = [_numeric_value(m.group()) for m in _NUMBER.finditer(remaining)]
        if len(values) > 1 or any(v is None or v <= 0 for v in values):
            return None
        return [ParsedSet(values[0] if values else None, reps) for _ in range(count)]

    cleaned = _REP_WORDS.sub(" ", chunk)
    cleaned = re.sub(r"\b(?:x|mal|à)\b", " ", cleaned, flags=re.I)
    values = [_numeric_value(m.group()) for m in _NUMBER.finditer(cleaned)]
    if not values or any(value is None for value in values):
        return None
    if not cleaned.strip() or not re.fullmatch(r"[\s+\d.,-]+", cleaned):
        return None

    if (
        allow_bodyweight and not chunk.lstrip().startswith("+")
        and len(values) <= 10 and all(1 <= value <= 30 for value in values)
    ):
        return [ParsedSet(None, int(value)) for value in values]
    if len(values) == 1 and chunk.lstrip().startswith("+"):
        return None
    if len(values) == 1:
        return None
    weight = values[0]
    reps = values[1:]
    if chunk.lstrip().startswith("+"):
        weight = values[0]
    if weight <= 0 or weight > 10000 or any(not 1 <= value <= 30 for value in reps):
        return None
    return [ParsedSet(weight, int(value)) for value in reps]


def parse_set_message(
    text: str,
    aliases: dict[str, list[str]] | None = None,
) -> ParsedLog | None:
    """Parst eine klare Satzmeldung; mehrdeutige Übungsnamen bleiben offen."""
    if not isinstance(text, str) or not text.strip():
        return None
    normalized_text = unicodedata.normalize("NFKC", text).strip()
    low = " " + normalize_alias(normalized_text) + " "
    if "?" in normalized_text or any(term in low for term in _REJECTION_TERMS):
        return None
    if has_non_exercise_start(normalized_text):
        return None

    mapping = builtin_aliases()
    if aliases:
        for key, values in aliases.items():
            normalized_key = normalize_alias(key)
            mapping[normalized_key] = list(dict.fromkeys(values))

    original_name, tail, equipment, exercise, ambiguous, fuzzy, known = _prefix_and_remainder(
        normalized_text, mapping
    )
    if not tail:
        return None
    # A comma followed by whitespace separates set pairs. A decimal comma does not.
    chunks = re.split(r",\s+", tail)
    parsed_sets = []
    bodyweight = known and is_pullup_name(exercise) and equipment is None
    for chunk in chunks:
        parsed = _parse_chunk(chunk, allow_bodyweight=bodyweight)
        if not parsed and parsed_sets:
            # Nach einer Last kann ein einzelner Wert ein weiterer Satz mit derselben Last sein.
            cleaned = _REP_WORDS.sub(" ", chunk)
            cleaned = re.sub(r"\b(?:x|mal|à)\b", " ", cleaned, flags=re.I)
            values = [_numeric_value(m.group()) for m in _NUMBER.finditer(cleaned)]
            if len(values) == 1 and values[0] is not None and 1 <= values[0] <= 30:
                previous_weight = parsed_sets[-1].weight_kg
                if bodyweight or previous_weight is not None:
                    parsed = [ParsedSet(previous_weight, int(values[0]))]
        if not parsed:
            return None
        parsed_sets.extend(parsed)
    if not 1 <= len(parsed_sets) <= 20:
        return None
    if known and not is_pullup_name(exercise) and any(
        item.weight_kg is None for item in parsed_sets
    ):
        return None
    if not ambiguous and exercise is None:
        exercise = " ".join(original_name.split())
        if not exercise:
            return None
    load_mode = "added" if is_pullup_name(exercise) and any(s.weight_kg is not None for s in parsed_sets) else "external"
    if is_pullup_name(exercise) and all(s.weight_kg is None for s in parsed_sets):
        load_mode = "bodyweight"
    return ParsedLog(
        exercise=exercise,
        sets=tuple(parsed_sets),
        equipment=equipment,
        load_mode=load_mode,
        known=known,
        alias=original_name or None,
        fuzzy=fuzzy,
        ambiguous=ambiguous,
    )

