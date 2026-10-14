"""Schneller, enger Parser für Krafttrainings-Sätze im Chat."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from domains.gym.aliases import (
    builtin_aliases, is_pullup_name, normalize_alias, resolve_alias,
    split_equipment_variant,
)
from domains.gym.legacy import legacy_exercise_for_alias


_NUMBER = re.compile(r"(?<![\w.])[+]?\d+(?:[.,]\d+)?(?![\d.])")
_EQUIPMENT = (
    (re.compile(r"^(?:kh|kurzhantel(?:n)?)\b[\s-]*", re.I), "dumbbell"),
    (re.compile(r"^(?:lh|langhantel(?:n)?)\b[\s-]*", re.I), "barbell"),
    (re.compile(r"^(?:sz(?:-?stange)?)\b[\s-]*", re.I), "ez_bar"),
    (re.compile(r"^(?:kabel|maschine|maschinen)\b[\s-]*", re.I), "machine"),
)
_REJECTION_TERMS = (
    "pound", " lb", "umrechnen", "wie viel", "wieviel", "wie schwer",
    "gelaufen", "laufen", "joggen", "laufband", "ich bin", "ich wiege",
    "kalorie", "kcal",
)
_REP_WORDS = re.compile(
    r"\b(?:kg|kilo|wdh\.?|wiederholungen?|reps?|saetze?|sätze?)\b", re.I
)
_SET_WORDS = re.compile(
    r"(?<!\w)(\d{1,2})\s*(?:saetze?|sätze?)\s*(?:à|x)?\s*(\d{1,2})\b",
    re.I,
)
_REP_MARKERS = re.compile(r"\b(?:wdh\.?|wiederholungen?|reps?)\b", re.I)
_NON_EXERCISE_START = re.compile(
    r"^(?:heute|morgen|gestern|ich|wir|du|ihr|er|sie|es|schreib(?:e)?|loesch(?:e)?|"
    r"entferne|formuliere|verfasse|mein(?:e|er|en|em|es)?|"
    r"unser(?:e|er|en|em|es)?|die|der|das|ein(?:e|er|en|em|es)?)\b"
)
_NEGATIVE_NUMBER = re.compile(r"(?<!\w)-\s*\d+(?:[.,]\d+)?")
_TIME_TOKEN = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_DATE_TOKEN = re.compile(
    r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2})(?!\d)"
)
_SHORT_DATE_TOKEN = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})(?P<separator>[./-])"
    r"(?P<month>\d{1,2})(?![\d.])"
)
_WARMUP_TOKEN = re.compile(r"\b(?:warm\s*up|warmup|aufwärm\w*)\b", re.I)


def _has_date_token(text: str) -> bool:
    if _DATE_TOKEN.search(text):
        return True
    slash_list = re.search(r"(?<!\d)\d{1,2}(?:\s*/\s*\d{1,2}){2,}(?!\d)", text)
    for match in _SHORT_DATE_TOKEN.finditer(text):
        day = int(match.group("day"))
        month = int(match.group("month"))
        if not 1 <= day <= 31 or not 1 <= month <= 12:
            continue
        if slash_list and slash_list.start() <= match.start() < slash_list.end():
            continue
        if _NUMBER.search(text[:match.start()]):
            continue
        if match.group("separator") in "/-" or len(match.group("month")) == 2:
            return True
    return False


@dataclass(frozen=True)
class ParsedSet:
    weight_kg: float | None
    reps: int
    is_warmup: bool = False


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


def _candidate_equipment(name: str) -> str | None:
    base, equipment = split_equipment_variant(name)
    if equipment:
        return equipment
    legacy = legacy_exercise_for_alias(base)
    return legacy.equipment if legacy else None


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
        if ambiguous and equipment:
            compatible = tuple(
                choice for choice in ambiguous
                if _candidate_equipment(choice) == equipment
            )
            if len(compatible) == 1:
                exercise, ambiguous = compatible[0], ()
            elif compatible:
                ambiguous = compatible
        return original_alias, rest, equipment, exercise, ambiguous, fuzzy, bool(exercise)

    first_number = _NUMBER.search(remainder)
    if not first_number:
        return remainder, "", equipment, None, (), False, False
    raw_name = remainder[:first_number.start()].strip(" ,:-")
    tail = remainder[first_number.start():].strip()
    if not raw_name:
        return "", tail, equipment, None, (), False, False
    base_name, suffix_equipment = split_equipment_variant(raw_name)
    if suffix_equipment:
        if equipment and equipment != suffix_equipment:
            return raw_name, "", equipment, None, (), False, False
        raw_name = base_name
        equipment = equipment or suffix_equipment
    exercise, ambiguous, fuzzy = resolve_alias(raw_name, aliases)
    return raw_name, tail, equipment, exercise, ambiguous, fuzzy, bool(exercise)


def _rep_values(text: str) -> list[int] | None:
    """Liest ausschließlich ganzzahlige Wiederholungen mit klaren Trennern."""
    value = _REP_WORDS.sub(" ", text)
    value = re.sub(r"(?<=\d)\s*:\s*(?=\d)", " ", value)
    value = value.strip().rstrip("!;.")
    if not value:
        return None
    # Ein einzelnes Kommapaar kann eine Dezimalzahl sein (12,5), keine sichere Wdh-Liste.
    if re.fullmatch(r"\d{1,2},\d{1,2}", value):
        return None
    if not re.fullmatch(r"[\s,/;x:-]*\d{1,2}(?:[\s,/;x:-]+\d{1,2})*", value, re.I):
        return None
    numbers = [int(item) for item in re.findall(r"\d+", value)]
    if not numbers or any(not 1 <= item <= 30 for item in numbers):
        return None
    return numbers


def _parse_chunk(chunk: str, allow_bodyweight: bool) -> list[ParsedSet] | None:
    chunk = chunk.strip().replace("×", "x").rstrip("!;.")
    if not chunk:
        return None
    if re.search(r"\d+\s*mal\b", chunk, re.I) and not re.search(
        r"\b(?:wdh|wiederholungen?|reps?|saetze?|sätze?)\b", chunk, re.I
    ):
        return None

    cleaned = re.sub(r"(?<=\d)\s*(?:kg|kilo)\b", " ", chunk, flags=re.I)
    cleaned = re.sub(r"(?<!\w)\+\s+(?=\d)", "+", cleaned)
    cleaned = _REP_WORDS.sub(" ", cleaned)
    cleaned = re.sub(r"(?<=\d)\s*:\s*(?=\d)", " ", cleaned)
    cleaned = cleaned.strip()

    # Gewicht gefolgt von Satzanzahl × Wiederholungen.
    weighted_set_rep = re.fullmatch(
        r"([+]?\d+(?:[.,]\d+)?)\s+(\d{1,2})\s*x\s*(\d{1,2})",
        cleaned,
        re.I,
    )
    if weighted_set_rep:
        weight = _numeric_value(weighted_set_rep.group(1))
        count, reps = int(weighted_set_rep.group(2)), int(weighted_set_rep.group(3))
        if weight is None or not 0 < weight <= 10000 or not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        return [ParsedSet(weight, reps) for _ in range(count)]

    # Kompakt: Gewicht × Wiederholungen × Sätze.
    compact_weighted_set_rep = re.fullmatch(
        r"([+]?\d+(?:[.,]\d+)?)\s*x\s*(\d{1,2})\s*x\s*(\d{1,2})",
        cleaned,
        re.I,
    )
    if compact_weighted_set_rep:
        weight = _numeric_value(compact_weighted_set_rep.group(1))
        reps, count = int(compact_weighted_set_rep.group(2)), int(compact_weighted_set_rep.group(3))
        if weight is None or not 0 < weight <= 10000 or not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        return [ParsedSet(weight, reps) for _ in range(count)]

    # Ebenfalls eindeutig: Sätze × Wiederholungen, danach das Gewicht.
    set_rep_weight = re.fullmatch(
        r"(\d{1,2})\s*x\s*(\d{1,2})\s+([+]?\d+(?:[.,]\d+)?)",
        cleaned,
        re.I,
    )
    if set_rep_weight:
        count, reps = int(set_rep_weight.group(1)), int(set_rep_weight.group(2))
        weight = _numeric_value(set_rep_weight.group(3))
        if weight is None or not 0 < weight <= 10000 or not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        return [ParsedSet(weight, reps) for _ in range(count)]

    # Ein explizites kg gehört zur ersten Zahl: "10 kg x 8" ist Last × Wdh.,
    # nie eine Satzanzahl; "3x8 kg" bleibt unklar.
    has_kg = bool(re.search(r"(?<=\d)\s*(?:kg|kilo)\b", chunk, re.I))
    if has_kg and not re.match(r"\s*[+]?\d+(?:[.,]\d+)?\s*(?:kg|kilo)\b", chunk, re.I):
        return None

    # Setanzahl × Wiederholungen ohne zusätzliche Last.
    set_rep = re.fullmatch(r"(\d{1,2})\s*x\s*(\d{1,2})", cleaned, re.I)
    if set_rep and not has_kg and int(set_rep.group(1)) <= 10:
        count, reps = int(set_rep.group(1)), int(set_rep.group(2))
        if not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        return [ParsedSet(None, reps) for _ in range(count)]

    # Explizite Schreibweise wie "80 kg 3 Sätze à 8 Wiederholungen".
    set_words = _SET_WORDS.search(chunk)
    if set_words:
        count, reps = int(set_words.group(1)), int(set_words.group(2))
        if not 1 <= count <= 10 or not 1 <= reps <= 30:
            return None
        remaining = chunk[:set_words.start()] + " " + chunk[set_words.end():]
        remaining = _REP_WORDS.sub(" ", remaining)
        remaining = remaining.strip()
        if not remaining:
            weight = None
        elif re.fullmatch(r"\+?\d+(?:[.,]\d+)?", remaining):
            weight = _numeric_value(remaining)
        else:
            return None
        if weight is not None and weight <= 0:
            return None
        return [ParsedSet(weight, reps) for _ in range(count)]

    # Ein kompaktes Komma-Triplett ohne vorheriges Gewicht bleibt bei bekannten
    # Nicht-Klimmzug-Übungen mehrdeutig und wird nicht als kg interpretiert.
    if re.fullmatch(r"\s*\d{1,2}(?:,\d{1,2}){2,}\s*", cleaned):
        if not allow_bodyweight:
            return None
        body_reps = _rep_values(cleaned)
        return [ParsedSet(None, reps) for reps in body_reps] if body_reps else None

    if allow_bodyweight and not cleaned.lstrip().startswith("+"):
        body_reps = _rep_values(cleaned)
        if body_reps:
            return [ParsedSet(None, reps) for reps in body_reps]

    match = _NUMBER.search(cleaned)
    if not match:
        return None
    weight = _numeric_value(match.group())
    if weight is None or not 0 < weight <= 10000:
        return None
    remainder = cleaned[:match.start()] + " " + cleaned[match.end():]
    if re.match(r"^\s*\d{1,2}\s*x.*x", remainder, re.I) or re.match(
        r"^\s*x\s+\d{1,2}\s*x", remainder, re.I
    ):
        return None
    reps = _rep_values(remainder)
    if not reps:
        return None
    return [ParsedSet(weight, count) for count in reps]


def parse_set_message(
    text: str,
    aliases: dict[str, list[str]] | None = None,
) -> ParsedLog | None:
    """Parst eine klare Satzmeldung; mehrdeutige Übungsnamen bleiben offen."""
    if not isinstance(text, str) or not text.strip():
        return None
    normalized_text = unicodedata.normalize("NFKC", text).strip()
    if (
        _NEGATIVE_NUMBER.search(normalized_text)
        or _TIME_TOKEN.search(normalized_text)
        or _has_date_token(normalized_text)
    ):
        return None
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
    suffix_equipment = re.match(
        r"^(kh|kurzhantel(?:n)?|lh|langhantel(?:n)?|sz(?:-?stange)?|kabel|cable|maschine|maschinen)\b[\s-]*",
        tail,
        re.I,
    )
    if suffix_equipment:
        suffix = normalize_alias(suffix_equipment.group(1))
        suffix_kind = (
            "dumbbell" if suffix.startswith(("kh", "kurzhantel")) else
            "barbell" if suffix.startswith(("lh", "langhantel")) else
            "machine" if suffix.startswith(("kabel", "cable", "maschine")) else "ez_bar"
        )
        if equipment and equipment != suffix_kind:
            return None
        equipment = suffix_kind
        tail = tail[suffix_equipment.end():]
    # Ein aufgelöster Datenbankname kann selbst ein Gerätepräfix oder -suffix tragen.
    if exercise and not ambiguous:
        base_name, embedded_equipment = split_equipment_variant(exercise)
        if embedded_equipment:
            if equipment and equipment != embedded_equipment:
                return None
            equipment = equipment or embedded_equipment
            exercise = base_name
    if ambiguous and equipment:
        matching = [
            choice for choice in ambiguous
            if split_equipment_variant(choice)[1] == equipment
        ]
        generic = [
            choice for choice in ambiguous
            if split_equipment_variant(choice)[1] is None
        ]
        choices = matching or (generic if len(generic) == 1 else [])
        if len(choices) == 1:
            exercise, embedded_equipment = split_equipment_variant(choices[0])
            equipment = equipment or embedded_equipment
            ambiguous = ()
            known = True
        elif choices:
            ambiguous = tuple(choices)
    if re.search(r"(?<!\w)\+\s*\d", tail) and not (
        known and is_pullup_name(exercise)
    ):
        return None
    tail = re.sub(r"(?<=\d)\s*:\s*(?=\d)", " ", tail)
    # A comma followed by whitespace separates set pairs. A decimal comma does not.
    chunks = re.split(r",\s+", tail)
    parsed_sets = []
    bodyweight = known and is_pullup_name(exercise) and equipment is None
    for chunk in chunks:
        markers = list(_WARMUP_TOKEN.finditer(chunk))
        chunk_warmup = False
        if markers:
            if len(markers) != 1:
                return None
            marker = markers[0]
            before = chunk[:marker.start()].strip()
            after = chunk[marker.end():].strip()
            if before and after:
                return None
            chunk_warmup = True
            chunk = (before or after).strip()
        set_markers = list(re.finditer(r"\b(?:saetze?|sätze?)\b", chunk, re.I))
        set_words = _SET_WORDS.search(chunk)
        if len(set_markers) > 1 or bool(set_markers) != bool(set_words):
            return None
        for marker in re.finditer(r"\bà\b", chunk, re.I):
            if not set_words or not set_words.start() <= marker.start() < set_words.end():
                return None
        rep_markers = list(_REP_MARKERS.finditer(chunk))
        if len(rep_markers) > 1 or rep_markers and re.search(
            r"\d", chunk[rep_markers[0].end():]
        ):
            return None
        kg_markers = list(re.finditer(r"(?<![^\W\d_])(?:kg|kilo)\b", chunk, re.I))
        if kg_markers:
            marker = kg_markers[0]
            load_token = chunk[:marker.start()].strip()
            if len(kg_markers) != 1 or not re.fullmatch(
                r"\+?\s*\d+(?:[.,]\d+)?", load_token
            ):
                return None
        explicit_added_marker = bool(re.search(r"(?<=\d)\s*(?:kg|kilo)\b", chunk, re.I))
        parsed = _parse_chunk(chunk, allow_bodyweight=bodyweight and not explicit_added_marker)
        if not parsed and parsed_sets:
            # Nach einer Last kann ein einzelner Wert ein weiterer Satz mit derselben Last sein.
            reps = _rep_values(chunk)
            if reps and len(reps) == 1:
                previous_weight = parsed_sets[-1].weight_kg
                if bodyweight or previous_weight is not None:
                    parsed = [ParsedSet(previous_weight, reps[0])]
        if not parsed:
            return None
        parsed_sets.extend(
            ParsedSet(item.weight_kg, item.reps, chunk_warmup) for item in parsed
        )
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

