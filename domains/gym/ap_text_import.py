"""Parser und idempotenter DB-Import für AlphaProgression-Text-Exporte."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

from core import db
from domains import fitness
from domains.gym.aliases import load_aliases, normalize_alias, resolve_alias, split_equipment_variant
from domains.gym.legacy import legacy_exercise_for_alias
from domains.gym.session import _exercise_defaults


_BERLIN = ZoneInfo("Europe/Berlin")
_WORKOUT_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*h$", re.I)
_EXERCISE_START = re.compile(r"^(\d+)\.\s*(.+)$")
_EXERCISE_REPS = re.compile(r"^\d+\s*reps?$", re.I)
_SUPERSET = re.compile(r"^superset\b", re.I)
_NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_EQUIPMENT_NAMES = {
    "body weight": "bodyweight",
    "bodyweight": "bodyweight",
    "bodyweight only": "bodyweight",
    "no equipment": None,
    "none": None,
    "dumbbell": "dumbbell",
    "dumbbells": "dumbbell",
    "kurzhantel": "dumbbell",
    "kurzhanteln": "dumbbell",
    "barbell": "barbell",
    "barbells": "barbell",
    "langhantel": "barbell",
    "langhanteln": "barbell",
    "ez bar": "ez_bar",
    "sz stange": "ez_bar",
    "smith machine": "smith_machine",
    "smith": "smith_machine",
    "multipresse": "smith_machine",
    "machine": "machine",
    "machines": "machine",
    "cable": "cable",
    "cables": "cable",
    "kabel": "cable",
    "kabelzug": "cable",
    "kettlebell": "kettlebell",
    "kettlebells": "kettlebell",
}
_EQUIPMENT_LABELS = {
    "dumbbell": "Kurzhantel",
    "barbell": "Langhantel",
    "ez_bar": "SZ-Stange",
    "cable": "Kabel",
    "machine": "Maschine",
    "smith_machine": "Smith machine",
}
_LEGACY_EQUIPMENT_LABELS = {
    "dumbbell": "KH",
    "barbell": "LH",
    "ez_bar": "SZ",
    "cable": "Kabel",
    "machine": "Maschine",
}


@dataclass(frozen=True)
class APSet:
    """Ein ausgeführter Satz aus dem Export."""

    set_index: int
    weight_kg: float | None = None
    reps: int | None = None
    distance_km: float | None = None
    duration_s: int | None = None
    load_mode: str = "external"


@dataclass(frozen=True)
class APExercise:
    """Eine Übung samt Gerätebezeichnung, Einheit, Notiz und ausgeführten Sätzen."""

    name: str
    equipment: str | None
    unit: str
    sets: tuple[APSet, ...]
    note: str | None = None
    load_mode: str = "external"


@dataclass(frozen=True)
class APWorkout:
    """Ein Workout mit lokaler Startzeit und AlphaProgression-Titel."""

    title: str
    started_at: datetime
    duration_min: int | None
    exercises: tuple[APExercise, ...]


@dataclass(frozen=True)
class ImportReport:
    """Ergebnis oder Dry-Run-Vorschau des Imports."""

    workouts_created: int
    workouts_skipped: int
    exercises_created: int
    sets_created: int
    dry_run: bool = False
    new_workouts: tuple["WorkoutReportEntry", ...] = ()
    skipped_workouts: tuple["WorkoutReportEntry", ...] = ()
    unclear_workouts: tuple["WorkoutReportEntry", ...] = ()


@dataclass(frozen=True)
class WorkoutReportEntry:
    """Ein einzelnes Workout im Importbericht."""

    date: date
    title: str
    existing_title: str | None = None


SetSignature = tuple[str, float | None, int | None]


@dataclass
class _ExistingWorkout:
    title: str | None
    created_at: datetime | None
    sets: Counter[SetSignature]


def _cells(line: str, line_number: int) -> list[str]:
    try:
        return [cell.strip() for cell in next(csv.reader(
            StringIO(line), delimiter=";", quotechar='"', strict=True,
        ))]
    except (csv.Error, StopIteration) as exc:
        raise ValueError(f"Zeile {line_number}: ungültige CSV-Zeile") from exc


def _parse_duration_minutes(value: str, line_number: int) -> int:
    text = value.strip().casefold()
    match = re.fullmatch(r"(\d+):(\d{2})\s*hr", text)
    if match:
        hours, minutes = map(int, match.groups())
        if minutes >= 60:
            raise ValueError(f"Zeile {line_number}: ungültige Workout-Dauer {value!r}")
        return hours * 60 + minutes

    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*hr?(?:\s*(\d+)\s*min)?", text)
    if match:
        hours = float(match.group(1).replace(",", "."))
        extra_minutes = int(match.group(2) or 0)
        return round(hours * 60) + extra_minutes

    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*min", text)
    if match:
        return round(float(match.group(1).replace(",", ".")))
    raise ValueError(f"Zeile {line_number}: unbekannte Workout-Dauer {value!r}")


def _parse_number(value: str, line_number: int, field: str) -> float:
    text = value.strip()
    if not _NUMBER.fullmatch(text):
        raise ValueError(f"Zeile {line_number}: ungültiger Wert für {field}: {value!r}")
    return float(text.replace(",", "."))


def _parse_seconds(value: str, unit: str, line_number: int) -> int:
    text = value.strip()
    match = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if match:
        minutes, seconds = map(int, match.groups())
        if seconds >= 60:
            raise ValueError(f"Zeile {line_number}: ungültige Zeit {value!r}")
        return minutes * 60 + seconds
    amount = _parse_number(text, line_number, unit)
    seconds = amount * 60 if unit == "MINS" else amount
    return round(seconds)


def _exercise_line(cells: list[str], line_number: int) -> tuple[str, str | None, str | None] | None:
    if not cells:
        return None
    match = _EXERCISE_START.fullmatch(cells[0])
    if not match:
        return None
    parts = [part.strip() for part in match.group(2).split("·") if part.strip()]
    if not parts:
        raise ValueError(f"Zeile {line_number}: Übungsname fehlt")
    name = parts[0]
    equipment = None
    for part in parts[1:]:
        if _EXERCISE_REPS.fullmatch(part) or _SUPERSET.match(part):
            continue
        if equipment is None:
            equipment = part
    note = ";".join(cells[1:]).strip() or None
    return name, equipment, note


def _set_from_row(
    values: list[str], columns: tuple[str, ...], line_number: int,
) -> APSet | None:
    if len(values) != len(columns):
        raise ValueError(
            f"Zeile {line_number}: erwartet {len(columns)} Spalten, erhalten {len(values)}",
        )
    if any(value.strip() == "-" for value in values):
        return None

    row = dict(zip(columns, values, strict=True))
    try:
        set_index = int(row["#"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Zeile {line_number}: ungültiger Satzindex") from exc
    if set_index <= 0:
        raise ValueError(f"Zeile {line_number}: Satzindex muss positiv sein")

    weight_text = row.get("KG")
    weight = _parse_number(weight_text, line_number, "KG") if weight_text is not None else None
    load_mode = "external"
    if weight_text is not None and weight_text.startswith(("+", "-")):
        load_mode = "bodyweight" if weight == 0 else "added"
    reps = None
    if "REPS" in row:
        try:
            reps = int(row["REPS"])
        except ValueError as exc:
            raise ValueError(f"Zeile {line_number}: ungültige Wiederholungszahl") from exc
        if reps <= 0:
            raise ValueError(f"Zeile {line_number}: Wiederholungszahl muss positiv sein")
    distance = (
        _parse_number(row["KM"], line_number, "KM") if "KM" in row else None
    )
    duration = None
    if "SECS" in row:
        duration = _parse_seconds(row["SECS"], "SECS", line_number)
    elif "MINS" in row:
        duration = _parse_seconds(row["MINS"], "MINS", line_number)
    if weight is None and "REPS" in row:
        # AlphaProgression exportiert reine Körpergewichts-Wiederholungen ohne KG-Spalte.
        weight = 0.0
        load_mode = "bodyweight"
    return APSet(set_index, weight, reps, distance, duration, load_mode)


def _load_mode(columns: tuple[str, ...], sets: list[APSet]) -> str:
    if "KG" not in columns:
        return "bodyweight" if "REPS" in columns else "external"
    if any(item.load_mode == "added" for item in sets):
        return "added"
    if sets and all(item.load_mode == "bodyweight" for item in sets):
        return "bodyweight"
    return "external"


def parse_export(text: str) -> list[APWorkout]:
    """Parst einen AlphaProgression-Text-Export ohne Datenbankzugriff."""
    if not isinstance(text, str):
        raise TypeError("AlphaProgression-Export muss Text sein")

    workouts: list[APWorkout] = []
    current_workout: dict | None = None
    current_exercise: dict | None = None
    columns: tuple[str, ...] | None = None

    def finish_exercise() -> None:
        nonlocal current_exercise, columns
        if current_workout is not None and current_exercise is not None:
            set_rows = current_exercise["sets"]
            current_workout["exercises"].append(APExercise(
                name=current_exercise["name"],
                equipment=current_exercise["equipment"],
                unit=current_exercise["unit"],
                sets=tuple(set_rows),
                note=current_exercise["note"],
                load_mode=_load_mode(columns or (), set_rows),
            ))
        current_exercise = None
        columns = None

    def finish_workout() -> None:
        nonlocal current_workout
        finish_exercise()
        if current_workout is not None:
            workouts.append(APWorkout(
                title=current_workout["title"],
                started_at=current_workout["started_at"],
                duration_min=current_workout["duration_min"],
                exercises=tuple(current_workout["exercises"]),
            ))
        current_workout = None

    for line_number, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not raw_line.strip():
            continue
        cells = _cells(raw_line, line_number)
        if len(cells) == 3:
            start_match = _WORKOUT_TIME.fullmatch(cells[1])
            if start_match:
                finish_workout()
                day = date.fromisoformat(start_match.group(1))
                hour, minute = map(int, start_match.group(2).split(":"))
                clock = time(hour, minute)
                current_workout = {
                    "title": cells[0],
                    "started_at": datetime.combine(day, clock, tzinfo=_BERLIN),
                    "duration_min": _parse_duration_minutes(cells[2], line_number),
                    "exercises": [],
                }
                continue

        if _EXERCISE_START.fullmatch(cells[0] if cells else ""):
            if current_workout is None:
                raise ValueError(f"Zeile {line_number}: Übung ohne Workout-Kopf")
            finish_exercise()
            parsed = _exercise_line(cells, line_number)
            assert parsed is not None
            name, equipment, note = parsed
            current_exercise = {
                "name": name, "equipment": equipment, "note": note,
                "unit": "reps", "sets": [],
            }
            continue

        normalized_columns = tuple(cell.strip().upper() for cell in cells)
        allowed_headers = {
            ("#", "KG", "REPS"), ("#", "KG", "SECS"),
            ("#", "KM", "MINS"), ("#", "MINS"), ("#", "REPS"),
        }
        if normalized_columns in allowed_headers:
            if current_exercise is None:
                raise ValueError(f"Zeile {line_number}: Spaltenkopf ohne Übung")
            columns = normalized_columns
            current_exercise["unit"] = (
                "km" if "KM" in columns else "min" if "MINS" in columns or "SECS" in columns
                else "reps"
            )
            continue

        if current_workout is None or current_exercise is None or columns is None:
            raise ValueError(f"Zeile {line_number}: unerwartete Zeile außerhalb eines Satzblocks")
        item = _set_from_row(cells, columns, line_number)
        if item is not None:
            current_exercise["sets"].append(item)

    finish_workout()
    return workouts


def _equipment_code(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_alias(value)
    if normalized in _EQUIPMENT_NAMES:
        return _EQUIPMENT_NAMES[normalized]
    return normalized.replace(" ", "_") or None


def _row_base(row: dict) -> str:
    name = row.get("name") or ""
    base, embedded = split_equipment_variant(name)
    if embedded or base != name:
        return base
    match = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", name)
    if match:
        suffix = match.group(2)
        suffix_code = _equipment_code(suffix)
        row_code = _equipment_code(row.get("equipment"))
        known_equipment_suffix = normalize_alias(suffix) in _EQUIPMENT_NAMES
        if suffix_code and (suffix_code == row_code or (not row_code and known_equipment_suffix)):
            return match.group(1).strip()
    return name


def _same_family(row: dict, canonical_name: str) -> bool:
    row_base = _row_base(row)
    if normalize_alias(row_base) == normalize_alias(canonical_name):
        return True
    existing_legacy = legacy_exercise_for_alias(row_base)
    requested_legacy = legacy_exercise_for_alias(canonical_name)
    return bool(existing_legacy and existing_legacy == requested_legacy)


def _family_rows(rows: list[dict], canonical_name: str) -> list[dict]:
    return [row for row in rows if _same_family(row, canonical_name)]


def _row_equipment(row: dict) -> str | None:
    if row.get("equipment"):
        return _equipment_code(row["equipment"])
    _base, embedded = split_equipment_variant(row.get("name") or "")
    if embedded:
        return _equipment_code(embedded)
    suffix = re.fullmatch(r".+?\s+\(([^()]+)\)", row.get("name") or "")
    if suffix and normalize_alias(suffix.group(1)) in _EQUIPMENT_NAMES:
        return _equipment_code(suffix.group(1))
    legacy = legacy_exercise_for_alias(_row_base(row))
    return _equipment_code(legacy.equipment) if legacy else None


def _matching_rows(rows: list[dict], canonical_name: str, equipment: str | None) -> list[dict]:
    return [
        row for row in _family_rows(rows, canonical_name)
        if _row_equipment(row) == equipment
    ]


def _preferred_row(rows: list[dict], canonical_name: str) -> dict:
    return min(
        rows,
        key=lambda row: (
            normalize_alias(row.get("name")) != normalize_alias(canonical_name),
            row.get("id") is None,
            row.get("id") or 0,
        ),
    )


def _canonical_exercise(
    exercise: APExercise, aliases: dict[str, list[str]], rows: list[dict],
) -> tuple[str, str | None, dict | None]:
    raw_base, embedded_equipment = split_equipment_variant(exercise.name)
    equipment = _equipment_code(exercise.equipment) or _equipment_code(embedded_equipment)
    raw_legacy = legacy_exercise_for_alias(raw_base)
    raw_canonical = raw_legacy.name if raw_legacy else raw_base
    source_alias = _source_alias(exercise)
    resolved, ambiguous, _fuzzy = resolve_alias(
        source_alias, aliases, allow_typo=False,
    )
    explicit_full_alias = source_alias != exercise.name and resolved is not None
    if not resolved and not ambiguous:
        resolved, ambiguous, _fuzzy = resolve_alias(
            raw_base, aliases, allow_typo=False,
        )
    candidates = [resolved] if resolved else list(ambiguous)
    raw_option = (raw_canonical, equipment or (
        _equipment_code(raw_legacy.equipment) if raw_legacy else None
    ))
    alias_options = []
    for candidate in candidates:
        candidate_base, candidate_embedded = split_equipment_variant(candidate)
        legacy = legacy_exercise_for_alias(candidate_base)
        canonical = legacy.name if legacy else candidate_base
        candidate_equipment = _equipment_code(candidate_embedded)
        target_equipment = equipment or candidate_equipment
        if target_equipment is None and legacy:
            target_equipment = _equipment_code(legacy.equipment)
        alias_options.append((canonical, target_equipment, candidate_equipment))
    if explicit_full_alias:
        # Ein expliziter Vollalias benennt das Ziel direkt. Seine gespeicherte
        # Übung darf nicht von der Geräteausstattung oder dem Rohname verdrängt werden.
        for canonical, target_equipment, *_rest in alias_options:
            matches = _family_rows(rows, canonical)
            if matches:
                return canonical, equipment or target_equipment, _preferred_row(matches, canonical)
        options = alias_options
    else:
        options = (
            [*alias_options, raw_option]
            if source_alias != exercise.name else [raw_option, *alias_options]
        )

    # Eine schon vorhandene, explizite Variante für das importierte Gerät hat Vorrang.
    exact_matches = []
    for canonical, target_equipment, *_rest in options:
        if target_equipment is not None:
            exact_matches.extend(_matching_rows(rows, canonical, target_equipment))
    if exact_matches:
        canonical = next(
            (name for name, _target, *_rest in options
             if any(_same_family(row, name) for row in exact_matches)),
            raw_canonical,
        )
        return canonical, equipment or next(
            target for name, target, *_rest in options
            if name == canonical and target is not None
        ), _preferred_row(exact_matches, canonical)

    # Wenn es keine gerätegleiche Variante gibt, bleibt der vorhandene Übungsname
    # maßgeblich. Das Importgerät darf die Metadaten der Bestandsübung nicht ändern.
    for canonical, target_equipment, *_rest in options:
        matches = _family_rows(rows, canonical)
        if matches:
            return canonical, equipment or target_equipment, _preferred_row(matches, canonical)

    unique = list(dict.fromkeys((canonical, target) for canonical, target, *_rest in options))
    if len(unique) == 1:
        canonical, target_equipment = unique[0]
        return canonical, target_equipment, None

    if raw_legacy:
        target_equipment = equipment or _equipment_code(raw_legacy.equipment)
        return raw_legacy.name, target_equipment, None
    return raw_base, equipment, None


def _variant_name(canonical_name: str, equipment: str | None, rows: list[dict]) -> str:
    family = [row for row in rows if _same_family(row, canonical_name)]
    if not family:
        return canonical_name
    legacy = legacy_exercise_for_alias(canonical_name)
    label = (
        _LEGACY_EQUIPMENT_LABELS.get(equipment)
        if legacy else None
    ) or _EQUIPMENT_LABELS.get(equipment or "") or (
        (equipment or "Equipment").replace("_", " ").title()
    )
    return f"{canonical_name} ({label})"


def _source_alias(exercise: APExercise) -> str:
    equipment = _equipment_code(exercise.equipment)
    if exercise.equipment and equipment:
        return f"{exercise.name} · {exercise.equipment}"
    return exercise.name


def _add_alias(cur, exercise: APExercise, exercise_id: int) -> None:
    alias = _source_alias(exercise)
    cur.execute(
        """INSERT INTO exercise_aliases (alias, normalized_alias, exercise_id)
           VALUES (%s,%s,%s) ON CONFLICT (normalized_alias) DO NOTHING""",
        (alias, normalize_alias(alias), exercise_id),
    )


def _insert_exercise(cur, name: str, equipment: str | None, item: APExercise) -> dict:
    defaults = _exercise_defaults(name, equipment, item.load_mode)
    has_distance = any(row.distance_km is not None for row in item.sets)
    category = "cardio" if has_distance else "strength"
    cur.execute(
        """INSERT INTO exercises
             (name,category,muscle,unit,exercise_type,equipment,rep_min,rep_max,
              rep_start,load_mode,weight_increment_kg,dumbbell_pair)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (name) DO NOTHING
           RETURNING id,name,equipment,unit,load_mode""",
        (name, category, fitness.guess_muscle(name), item.unit,
         defaults["exercise_type"], equipment, defaults["rep_min"],
         defaults["rep_max"], defaults["rep_start"], defaults["load_mode"],
         defaults["weight_increment_kg"], defaults["dumbbell_pair"]),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id,name,equipment,unit,load_mode FROM exercises WHERE name=%s",
            (name,),
        )
        row = cur.fetchone()
    return dict(row)


def _workout_notes(workout: APWorkout) -> str | None:
    notes = [
        f"{item.name}: {item.note}"
        for item in workout.exercises if item.note
    ]
    return "\n".join(notes) or None


def _berlin_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_BERLIN)
    return value.astimezone(_BERLIN)


def _set_signature(
    exercise_key: str, weight_kg: float | None, reps: int | None,
) -> SetSignature | None:
    if weight_kg is None and reps is None:
        return None
    weight = round(float(weight_kg), 3) if weight_kg is not None else None
    return exercise_key, weight, int(reps) if reps is not None else None


def _existing_workouts(rows: list[dict]) -> list[_ExistingWorkout]:
    by_id: dict[int, _ExistingWorkout] = {}
    for row in rows:
        workout_id = row["workout_id"]
        workout = by_id.setdefault(workout_id, _ExistingWorkout(
            title=row.get("title"), created_at=row.get("created_at"), sets=Counter(),
        ))
        exercise_id = row.get("exercise_id")
        if exercise_id is None:
            continue
        signature = _set_signature(
            f"id:{exercise_id}", row.get("weight_kg"), row.get("reps"),
        )
        if signature is not None:
            workout.sets[signature] += 1
    return list(by_id.values())


def _workout_set_signatures(
    workout: APWorkout, aliases: dict[str, list[str]], exercise_rows: list[dict],
) -> Counter[SetSignature]:
    signatures: Counter[SetSignature] = Counter()
    for item in workout.exercises:
        if not item.sets:
            continue
        canonical, equipment, exercise_row = _canonical_exercise(
            item, aliases, exercise_rows,
        )
        if exercise_row is not None:
            if exercise_row.get("id") is not None:
                exercise_key = f"id:{exercise_row['id']}"
            else:
                exercise_key = f"name:{normalize_alias(exercise_row['name'])}"
        else:
            storage_name = _variant_name(canonical, equipment, exercise_rows)
            exercise_key = f"name:{normalize_alias(storage_name)}"
        for set_item in item.sets:
            signature = _set_signature(
                exercise_key, set_item.weight_kg, set_item.reps,
            )
            if signature is not None:
                signatures[signature] += 1
    return signatures


def _duplicate_status(
    workout: APWorkout, existing: list[_ExistingWorkout],
    ap_sets: Counter[SetSignature],
) -> tuple[str, str | None, list[str | None]]:
    exact_title = next(
        (item for item in existing if item.title == workout.title), None,
    )
    if exact_title is not None:
        return "skipped", exact_title.title, []

    ap_started_at = _berlin_datetime(workout.started_at)
    nearby: list[tuple[float, _ExistingWorkout]] = []
    for item in existing:
        if item.created_at is None:
            continue
        existing_started_at = _berlin_datetime(item.created_at)
        if existing_started_at.date() != ap_started_at.date():
            continue
        difference = abs((existing_started_at - ap_started_at).total_seconds())
        if difference <= 90 * 60:
            nearby.append((difference, item))

    ap_set_count = sum(ap_sets.values())
    if ap_set_count:
        matching = []
        for difference, item in nearby:
            overlap = sum((ap_sets & item.sets).values())
            if overlap * 2 >= ap_set_count:
                matching.append((overlap / ap_set_count, difference, item.title))
        if matching:
            _ratio, _difference, title = max(
                matching, key=lambda item: (item[0], -item[1]),
            )
            return "skipped", title, []

    if nearby:
        titles = [item.title for _difference, item in sorted(
            nearby, key=lambda item: item[0],
        )]
        return "unclear", None, titles
    return "new", None, []


def import_workouts(
    workouts: list[APWorkout] | tuple[APWorkout, ...], *, dry_run: bool = False,
) -> ImportReport:
    """Importiert bereits geparste Workouts atomar und idempotent."""
    alias_map = load_aliases()
    workouts_created = workouts_skipped = exercises_created = sets_created = 0
    new_workouts: list[WorkoutReportEntry] = []
    skipped_workouts: list[WorkoutReportEntry] = []
    unclear_workouts: list[WorkoutReportEntry] = []
    planned_by_date: dict[date, list[_ExistingWorkout]] = {}

    with db.cursor() as cur:
        # Serialisiert parallele AP-Imports; Duplikatprüfung und Inserts bleiben atomar.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (0x41505458,))
        cur.execute("SELECT id,name,equipment,unit,load_mode FROM exercises")
        exercise_rows = [dict(row) for row in cur.fetchall()]

        for workout in workouts:
            started_at = _berlin_datetime(workout.started_at)
            workout_date = started_at.date()
            cur.execute(
                """SELECT w.id AS workout_id,w.title,w.created_at,
                          ws.exercise_id,ws.weight_kg,ws.reps
                   FROM workouts w
                   LEFT JOIN workout_sets ws ON ws.workout_id=w.id
                   WHERE w.date=%s
                   ORDER BY w.id,ws.id""",
                (workout_date,),
            )
            existing = _existing_workouts(cur.fetchall())
            if dry_run:
                existing.extend(planned_by_date.get(workout_date, ()))
            ap_sets = _workout_set_signatures(workout, alias_map, exercise_rows)
            status, matched_title, unclear_titles = _duplicate_status(
                workout, existing, ap_sets,
            )
            if status == "skipped":
                workouts_skipped += 1
                skipped_workouts.append(WorkoutReportEntry(
                    workout_date, workout.title, matched_title,
                ))
                continue
            if status == "unclear":
                unclear_workouts.extend(
                    WorkoutReportEntry(workout_date, workout.title, title)
                    for title in unclear_titles
                )
                continue

            workouts_created += 1
            new_workouts.append(WorkoutReportEntry(workout_date, workout.title))
            notes = _workout_notes(workout)
            completed_at = started_at + timedelta(minutes=workout.duration_min or 0)
            if not dry_run:
                cur.execute(
                    """INSERT INTO workouts
                         (date,title,type,duration_min,notes,created_at,last_activity_at,ended_at)
                       VALUES (%s,%s,'strength',%s,%s,%s,%s,%s) RETURNING id""",
                    (workout_date, workout.title, workout.duration_min, notes,
                     started_at, completed_at, completed_at),
                )
                workout_id = cur.fetchone()["id"]
            else:
                workout_id = None

            for item in workout.exercises:
                if not item.sets:
                    continue
                canonical, equipment, exercise_row = _canonical_exercise(
                    item, alias_map, exercise_rows,
                )
                if exercise_row is None:
                    storage_name = _variant_name(canonical, equipment, exercise_rows)
                    exercise_row = next(
                        (row for row in exercise_rows
                         if normalize_alias(row["name"]) == normalize_alias(storage_name)),
                        None,
                    )
                    if exercise_row is None:
                        exercises_created += 1
                        if not dry_run:
                            exercise_row = _insert_exercise(cur, storage_name, equipment, item)
                        else:
                            exercise_row = {
                                "id": None, "name": storage_name,
                                "equipment": equipment, "unit": item.unit,
                                "load_mode": item.load_mode,
                            }
                        exercise_rows.append(exercise_row)
                if exercise_row.get("id") is None:
                    # Planned dry-run exercises still need a stable identity for later items.
                    same_row = next(
                        (row for row in exercise_rows[:-1]
                         if normalize_alias(row["name"]) == normalize_alias(exercise_row["name"])),
                        None,
                    )
                    if same_row is not None:
                        exercise_row = same_row
                if not dry_run:
                    _add_alias(cur, item, exercise_row["id"])
                    for set_item in item.sets:
                        cur.execute(
                            """INSERT INTO workout_sets
                                 (workout_id,exercise_id,set_index,reps,weight_kg,distance_km,duration_s)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (workout_id, exercise_row["id"], set_item.set_index,
                             set_item.reps, set_item.weight_kg,
                             set_item.distance_km, set_item.duration_s),
                        )
                sets_created += len(item.sets)

            if dry_run:
                planned_by_date.setdefault(workout_date, []).append(_ExistingWorkout(
                    title=workout.title, created_at=started_at, sets=ap_sets,
                ))

    return ImportReport(
        workouts_created=workouts_created,
        workouts_skipped=workouts_skipped,
        exercises_created=exercises_created,
        sets_created=sets_created,
        dry_run=dry_run,
        new_workouts=tuple(new_workouts),
        skipped_workouts=tuple(skipped_workouts),
        unclear_workouts=tuple(unclear_workouts),
    )
