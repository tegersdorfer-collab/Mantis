"""Planmodell, AP-Historien-Seed und Workout-Kontext für den Gym-Tracker."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from statistics import median

from core import db
from domains.gym import session
from domains.gym.ap_text_import import APWorkout, resolve_exercise_row
from domains.gym.aliases import normalize_alias
from domains.gym.tracker import EXERCISES, REST_SECONDS, TrackerError


PLAN_DAY_ORDER = ("Lower A", "Upper A", "Lower B", "Upper B")
DEFAULT_PLAN_NAME = "New plan"
DEFAULT_REST_SECONDS = 180
SECONDS_RANGE_WIDTH = 10
MAX_REPS = 100
MAX_SECONDS = 600


def _date_value(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return date.min


def _week_one(title: str | None, plan_name: str) -> bool:
    if not title:
        return False
    marker = re.compile(
        rf"\bWeek\s+1\s*·\s*{re.escape(plan_name)}(?:\s*$|\s*·)", re.IGNORECASE
    )
    return bool(marker.search(str(title)))


def choose_plan_start_date(workouts: list[dict], plan_name: str = DEFAULT_PLAN_NAME) -> date:
    """Wählt Week 1 nach der letzten Historienlücke von mehr als 21 Tagen."""
    anchors = sorted(
        (_date_value(row.get("date")) for row in workouts if _week_one(row.get("title"), plan_name))
    )
    anchors = [value for value in anchors if value != date.min]
    if not anchors:
        raise TrackerError(409, f"Kein Workout mit 'Week 1 · {plan_name}' gefunden")
    start = anchors[0]
    previous = anchors[0]
    for current in anchors[1:]:
        if (current - previous).days > 21:
            start = current
        previous = current
    return start


def _start_date_reason(workouts: list[dict], plan_name: str, start: date) -> str:
    anchors = sorted(
        _date_value(row.get("date"))
        for row in workouts
        if _week_one(row.get("title"), plan_name) and _date_value(row.get("date")) != date.min
    )
    previous = next(
        (left for left, right in zip(anchors, anchors[1:], strict=False)
         if right == start and (right - left).days > 21),
        None,
    )
    if previous:
        gap = (start - previous).days
        return f"Week 1 am {start.isoformat()} liegt nach {gap} Tagen Abstand zur vorherigen Week 1 am {previous.isoformat()}."
    return f"Ältestes Week-1-Workout des Planblocks: {start.isoformat()}."


def _workout_sort_key(row: dict) -> tuple:
    workout_date = _date_value(row.get("date"))
    created = row.get("created_at")
    if isinstance(created, datetime):
        created_key = created.timestamp()
    else:
        created_key = 0
    try:
        workout_id = int(row.get("id") or row.get("workout_id") or 0)
    except (TypeError, ValueError):
        workout_id = 0
    return workout_date, created_key, workout_id


def _day_from_title(title: str | None, day_names=PLAN_DAY_ORDER) -> str | None:
    if not title:
        return None
    candidate = str(title).split("·", 1)[0].strip()
    return candidate if candidate in day_names else None


def _week_from_title(title: str | None) -> int | None:
    if not title:
        return None
    match = re.search(r"(?:^|·)\s*Week\s+(\d+)\s*·", str(title), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _day_index_from_title(title: str | None, day_names: list[str]) -> int | None:
    day_name = _day_from_title(title, day_names)
    if day_name is None:
        return None
    match = re.search(r"(?:^|·)\s*Day\s+(\d+)\s*·", str(title), re.IGNORECASE)
    if match:
        title_day_number = int(match.group(1))
        if 1 <= title_day_number <= len(day_names):
            return title_day_number - 1
    return day_names.index(day_name)


def _ordered_day_names(days: list[dict] | list[str]) -> list[str]:
    return [day["name"] if isinstance(day, dict) else str(day) for day in days]


def rotation_week_number(start_date, days: list[dict] | list[str], workouts: list[dict]) -> int:
    """Leitet die laufende Planwoche aus AP-Titeln oder Tagesrotation ab."""
    day_names = _ordered_day_names(days)
    if not day_names:
        return 1
    start = _date_value(start_date)
    ordered_workouts = sorted(
        (
            row for row in workouts
            if _date_value(row.get("date")) >= start
            and _day_index_from_title(row.get("title"), day_names) is not None
        ),
        key=_workout_sort_key,
    )
    if not ordered_workouts:
        return 1

    latest = ordered_workouts[-1]
    latest_index = _day_index_from_title(latest.get("title"), day_names)
    titled_week = _week_from_title(latest.get("title"))
    if titled_week is not None:
        return titled_week + int(latest_index == len(day_names) - 1)

    week = 1
    previous_index = None
    for workout in ordered_workouts:
        day_index = _day_index_from_title(workout.get("title"), day_names)
        if previous_index is not None and day_index <= previous_index:
            week += 1
        previous_index = day_index
    if previous_index == len(day_names) - 1:
        week += 1
    return week


def _rotation_next_day(days: list[dict], workouts: list[dict]) -> tuple[str | None, dict | None]:
    if not days:
        return None, None
    day_names = _ordered_day_names(days)
    last_day = next(
        (
            _day_from_title(row.get("title"), day_names)
            for row in sorted(workouts, key=_workout_sort_key, reverse=True)
            if _day_from_title(row.get("title"), day_names) is not None
        ),
        None,
    )
    last_index = day_names.index(last_day) if last_day is not None else -1
    next_day = days[(last_index + 1) % len(days)]
    return last_day, next_day


def _is_timed(source: dict, sets: list[dict]) -> bool:
    """Zeitbasierte Kraftübung: Einheit 'min', Sätze mit Dauer, ohne Strecke."""
    return (
        str(source.get("unit") or "").casefold() == "min"
        and bool(sets)
        and all(row.get("duration_s") is not None and row.get("distance_km") is None for row in sets)
    )


def _title_plan_name(title: str | None) -> str:
    """Planname am Titelende ('Upper A · Day 2 · Week 7 · <Planname>')."""
    return str(title or "").rsplit("·", 1)[-1].strip().casefold()


def _ap_rep_targets(cur, ap_workouts, plan_name: str, exercise_by_id, canonical_ids) -> dict:
    """Mappt AP-Exportziele ('x reps') je (Plantag, Übungs-ID); jüngstes Workout gewinnt."""
    if not ap_workouts:
        return {}
    from domains.gym.aliases import load_aliases

    aliases = load_aliases()
    rows = [dict(row) for row in exercise_by_id.values()]
    ordered = sorted(
        (w for w in ap_workouts if _title_plan_name(w.title) == plan_name.strip().casefold()
         and _day_from_title(w.title)),
        key=lambda w: w.started_at, reverse=True,
    )
    targets: dict[tuple[str, int], int] = {}
    for workout in ordered:
        day = _day_from_title(workout.title)
        for item in workout.exercises:
            if not item.target_reps:
                continue
            row = resolve_exercise_row(item, aliases, rows)
            if row is None or row.get("id") is None:
                continue
            exercise_id = canonical_ids.get(int(row["id"]), int(row["id"]))
            targets.setdefault((day, exercise_id), item.target_reps)
    return targets


def _seed_data(cur, plan_name: str, ap_workouts: list[APWorkout] | None = None) -> dict:
    cur.execute(
        """SELECT id,date,title,created_at FROM workouts
           WHERE type IN ('gym','strength') AND title ILIKE %s""",
        (f"%{plan_name}%",),
    )
    all_workouts = [
        dict(row) for row in cur.fetchall()
        if _title_plan_name(row.get("title")) == plan_name.strip().casefold()
    ]
    valid_workouts = [row for row in all_workouts if _day_from_title(row.get("title"))]
    start = choose_plan_start_date(all_workouts, plan_name)
    current_workouts = [row for row in valid_workouts if _date_value(row.get("date")) >= start]
    if not current_workouts:
        raise TrackerError(409, f"Keine Workouts nach dem Planstart für {plan_name} gefunden")

    workout_ids = [row["id"] for row in current_workouts]
    cur.execute(
        """SELECT e.id,e.name,e.category,e.unit,e.rep_min,e.rep_max,
                  e.equipment,e.load_mode
           FROM exercises e"""
    )
    exercise_by_id = {int(row["id"]): dict(row) for row in cur.fetchall()}
    cur.execute(
        "SELECT normalized_alias,exercise_id FROM exercise_aliases WHERE exercise_id IS NOT NULL"
    )
    alias_targets = {
        normalize_alias(row["normalized_alias"]): int(row["exercise_id"])
        for row in cur.fetchall()
    }
    canonical_ids = {
        exercise_id: alias_targets.get(normalize_alias(row["name"]), exercise_id)
        for exercise_id, row in exercise_by_id.items()
    }
    cur.execute(
        """SELECT ws.id,ws.workout_id,ws.exercise_id,ws.set_index,ws.reps,
                  ws.duration_s,ws.distance_km,
                  COALESCE(ws.is_warmup,FALSE) AS is_warmup
           FROM workout_sets ws WHERE ws.workout_id=ANY(%s)
           ORDER BY ws.workout_id,ws.set_index,ws.id""",
        (workout_ids,),
    )
    sets_by_workout: dict[int, list[dict]] = {}
    for row in cur.fetchall():
        row = dict(row)
        sets_by_workout.setdefault(int(row["workout_id"]), []).append(row)

    valid_by_day: dict[str, list[dict]] = {day: [] for day in PLAN_DAY_ORDER}
    counts_by_workout: dict[int, dict[int, int]] = {}
    ordered_exercises: dict[int, list[int]] = {}
    timed_ids: set[int] = set()
    warnings: list[str] = []
    warned_units: set[tuple[int, int]] = set()
    for workout in current_workouts:
        workout_id = int(workout["id"])
        day = _day_from_title(workout.get("title"))
        day_workouts = valid_by_day[day]
        day_workouts.append(workout)
        counts: dict[int, int] = {}
        exercise_order: list[int] = []
        for item in sets_by_workout.get(workout_id, []):
            if item["is_warmup"]:
                continue
            source_id = int(item["exercise_id"])
            source = exercise_by_id.get(source_id)
            if not source:
                continue
            if str(source.get("category") or "strength").casefold() == "cardio":
                continue
            exercise_id = canonical_ids.get(source_id, source_id)
            target = exercise_by_id.get(exercise_id)
            if not target:
                continue
            if str(target.get("category") or "strength").casefold() == "cardio":
                continue
            source_unit = str(source.get("unit") or "reps").casefold()
            canonical_unit = str(target.get("unit") or "reps").casefold()
            workout_rows = [
                row for row in sets_by_workout.get(workout_id, [])
                if int(row["exercise_id"]) == source_id and not row["is_warmup"]
            ]
            has_duration = any(
                row.get("duration_s") is not None and row.get("distance_km") is None
                for row in workout_rows
            )
            has_reps = any(row.get("reps") is not None for row in workout_rows)
            if source_unit not in {"reps", "min"} and canonical_unit != "min" and not has_duration:
                continue
            timed = canonical_unit == "min" or has_duration
            if timed:
                timed_ids.add(exercise_id)
            if (
                source_unit != canonical_unit
                or (canonical_unit == "min" and has_reps)
                or (canonical_unit != "min" and has_duration)
                or (has_duration and has_reps)
            ) and (source_id, exercise_id) not in warned_units:
                warnings.append(
                    f"Einheit widersprüchlich für '{source['name']}' → '{target['name']}': "
                    f"Quellübung '{source_unit}', kanonisch '{canonical_unit}', "
                    f"Satzdaten {'mit Sekunden' if has_duration else 'ohne Sekunden'}; "
                    f"Seed-Ziel ist {'seconds' if timed else 'reps'}."
                )
                warned_units.add((source_id, exercise_id))
            counts[exercise_id] = counts.get(exercise_id, 0) + 1
            if exercise_id not in exercise_order:
                exercise_order.append(exercise_id)
        counts_by_workout[workout_id] = counts
        ordered_exercises[workout_id] = exercise_order

    ap_targets = _ap_rep_targets(cur, ap_workouts, plan_name, exercise_by_id, canonical_ids)
    days = []
    for sort_order, day_name in enumerate(PLAN_DAY_ORDER):
        workouts = sorted(valid_by_day[day_name], key=_workout_sort_key, reverse=True)
        latest = workouts[0] if workouts else None
        exercise_ids = ordered_exercises.get(int(latest["id"]), []) if latest else []
        recent = workouts[:3]
        entries = []
        for exercise_sort, exercise_id in enumerate(exercise_ids):
            exercise = exercise_by_id[exercise_id]
            historical_counts = [
                counts_by_workout.get(int(workout["id"]), {}).get(exercise_id, 0)
                for workout in recent
            ]
            historical_counts = [count for count in historical_counts if count > 0]
            if historical_counts:
                target = int(Decimal(str(median(historical_counts))).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ))
            else:
                target = 3
            target_unit = "seconds" if exercise_id in timed_ids else "reps"
            if target_unit == "seconds":
                # Kein AP-Ziel für Haltezeiten: Median der letzten Sätze, auf 5 s gerundet,
                # plus 10 s Spielraum als Obergrenze.
                durations = [
                    int(row["duration_s"]) for row in sets_by_workout.get(int(latest["id"]), [])
                    if not row["is_warmup"] and row.get("duration_s") is not None
                    and canonical_ids.get(int(row["exercise_id"]), int(row["exercise_id"])) == exercise_id
                ]
                low = min(
                    MAX_SECONDS,
                    max(5, int(round(median(durations) / 5.0)) * 5),
                ) if durations else 30
                high = min(MAX_SECONDS, low + SECONDS_RANGE_WIDTH)
            elif (day_name, exercise_id) in ap_targets:
                # AP kennt genau ein Ziel: reines Ziel (min == max) statt erfundener Spanne.
                low = high = ap_targets[(day_name, exercise_id)]
            else:
                low = int(exercise.get("rep_min") or 6)
                high = int(exercise.get("rep_max") or 10)
            entries.append({
                "exercise_id": exercise_id,
                "name": exercise["name"],
                "sort_order": exercise_sort,
                "target_sets": max(1, min(20, target)),
                "rep_min": low,
                "rep_max": max(low, high),
                "rest_s": DEFAULT_REST_SECONDS,
                "target_unit": target_unit,
            })
        days.append({"name": day_name, "sort_order": sort_order, "exercises": entries})
    return {
        "name": plan_name,
        "start_date": start,
        "start_reason": _start_date_reason(all_workouts, plan_name, start),
        "warnings": warnings,
        "days": days,
    }


def preview_seed(
    plan_name: str = DEFAULT_PLAN_NAME, ap_workouts: list[APWorkout] | None = None,
) -> dict:
    """Liest AP-Workouts und leitet den Seed ohne Änderungen an der DB ab."""
    with db.cursor() as cur:
        return _seed_data(cur, plan_name, ap_workouts)


def seed_plan(
    plan_name: str = DEFAULT_PLAN_NAME, ap_workouts: list[APWorkout] | None = None,
) -> dict:
    """Ersetzt den benannten Seed-Plan atomar und aktiviert ihn idempotent."""
    with db.cursor() as cur:
        data = _seed_data(cur, plan_name, ap_workouts)
        cur.execute("UPDATE gym_plans SET active=FALSE WHERE active=TRUE")
        cur.execute(
            """INSERT INTO gym_plans (name,start_date,active)
               VALUES (%s,%s,TRUE)
               ON CONFLICT (name) DO UPDATE SET
                 start_date=EXCLUDED.start_date,active=TRUE,updated_at=NOW()
               RETURNING id""",
            (plan_name, data["start_date"]),
        )
        plan_id = int(cur.fetchone()["id"])
        cur.execute("DELETE FROM gym_plan_days WHERE plan_id=%s", (plan_id,))
        for day in data["days"]:
            cur.execute(
                """INSERT INTO gym_plan_days (plan_id,name,sort_order)
                   VALUES (%s,%s,%s) RETURNING id""",
                (plan_id, day["name"], day["sort_order"]),
            )
            day_id = int(cur.fetchone()["id"])
            for item in day["exercises"]:
                cur.execute(
                    """INSERT INTO gym_plan_exercises
                         (day_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (day_id, item["exercise_id"], item["sort_order"], item["target_sets"],
                     item["rep_min"], item["rep_max"], item["rest_s"], item["target_unit"]),
                )
    return get_plan(plan_id)


def _plan_row(plan_id: int) -> dict:
    row = db.query_one(
        "SELECT id,name,start_date,active,created_at,updated_at FROM gym_plans WHERE id=%s",
        (plan_id,),
    )
    if not row:
        raise TrackerError(404, "Plan nicht gefunden")
    row["id"] = int(row["id"])
    row["start_date"] = _date_value(row["start_date"]).isoformat()
    row["active"] = bool(row["active"])
    return row


def get_plan(plan_id: int) -> dict:
    plan = _plan_row(plan_id)
    plan["days"] = []
    days = db.query(
        "SELECT id,plan_id,name,sort_order FROM gym_plan_days WHERE plan_id=%s ORDER BY sort_order,id",
        (plan_id,),
    )
    for day in days:
        day["id"] = int(day["id"])
        day["plan_id"] = int(day["plan_id"])
        day["sort_order"] = int(day["sort_order"])
        day["exercises"] = db.query(
            """SELECT pe.id,pe.exercise_id,e.name,e.muscle,e.equipment,
                      pe.sort_order,pe.target_sets,pe.rep_min,pe.rep_max,pe.rest_s,pe.target_unit
               FROM gym_plan_exercises pe JOIN exercises e ON e.id=pe.exercise_id
               WHERE pe.day_id=%s ORDER BY pe.sort_order,pe.id""",
            (day["id"],),
        )
        for item in day["exercises"]:
            for key in ("id", "exercise_id", "sort_order", "target_sets", "rep_min", "rep_max", "rest_s"):
                item[key] = int(item[key])
        plan["days"].append(day)
    return plan


def active_plan() -> dict | None:
    row = db.query_one("SELECT id FROM gym_plans WHERE active=TRUE LIMIT 1")
    return get_plan(int(row["id"])) if row else None


def list_plans() -> list[dict]:
    rows = db.query(
        "SELECT id FROM gym_plans ORDER BY active DESC,updated_at DESC,id DESC"
    )
    return [get_plan(int(row["id"])) for row in rows]


def _validate_plan_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or len(value) > 100:
        raise TrackerError(400, "Der Planname muss 1 bis 100 Zeichen lang sein")
    return value


def _parse_start_date(value) -> date:
    try:
        return _date_value(value) if isinstance(value, (date, datetime)) else date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise TrackerError(400, "Ungültiges Startdatum") from None


def create_plan(name: str, start_date: date | str | None = None) -> dict:
    plan_name = _validate_plan_name(name)
    begins = _parse_start_date(start_date or date.today())
    active = not bool(db.query_one("SELECT id FROM gym_plans WHERE active=TRUE"))
    try:
        plan_id = db.insert_returning(
            """INSERT INTO gym_plans (name,start_date,active)
               VALUES (%s,%s,%s) RETURNING id""",
            (plan_name, begins, active),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Ein Plan mit diesem Namen existiert bereits") from None
        raise
    return get_plan(int(plan_id))


def update_plan(plan_id: int, *, name: str | None = None, start_date=None) -> dict:
    current = _plan_row(plan_id)
    new_name = _validate_plan_name(name) if name is not None else current["name"]
    new_start = _parse_start_date(start_date) if start_date is not None else date.fromisoformat(current["start_date"])
    try:
        db.execute(
            "UPDATE gym_plans SET name=%s,start_date=%s,updated_at=NOW() WHERE id=%s",
            (new_name, new_start, plan_id),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Ein Plan mit diesem Namen existiert bereits") from None
        raise
    return get_plan(plan_id)


def activate_plan(plan_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM gym_plans WHERE id=%s", (plan_id,))
        if not cur.fetchone():
            raise TrackerError(404, "Plan nicht gefunden")
        cur.execute("UPDATE gym_plans SET active=FALSE WHERE active=TRUE")
        cur.execute("UPDATE gym_plans SET active=TRUE,updated_at=NOW() WHERE id=%s", (plan_id,))
    return get_plan(plan_id)


def add_day(plan_id: int, name: str) -> dict:
    day_name = str(name or "").strip()
    if not day_name or len(day_name) > 80:
        raise TrackerError(400, "Der Tagesname muss 1 bis 80 Zeichen lang sein")
    if not db.query_one("SELECT id FROM gym_plans WHERE id=%s", (plan_id,)):
        raise TrackerError(404, "Plan nicht gefunden")
    order = db.query_one("SELECT COALESCE(MAX(sort_order),-1)+1 AS next FROM gym_plan_days WHERE plan_id=%s", (plan_id,))["next"]
    try:
        day_id = db.insert_returning(
            "INSERT INTO gym_plan_days (plan_id,name,sort_order) VALUES (%s,%s,%s) RETURNING id",
            (plan_id, day_name, order),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Dieser Tag existiert bereits") from None
        raise
    return next(day for day in get_plan(plan_id)["days"] if day["id"] == int(day_id))


def update_day(day_id: int, *, name: str | None = None, sort_order: int | None = None) -> dict:
    day = db.query_one("SELECT id,plan_id,name,sort_order FROM gym_plan_days WHERE id=%s", (day_id,))
    if not day:
        raise TrackerError(404, "Plantag nicht gefunden")
    new_name = str(name).strip() if name is not None else day["name"]
    if not new_name or len(new_name) > 80:
        raise TrackerError(400, "Der Tagesname muss 1 bis 80 Zeichen lang sein")
    new_order = day["sort_order"] if sort_order is None else sort_order
    if isinstance(new_order, bool) or not isinstance(new_order, int) or new_order < 0:
        raise TrackerError(400, "Ungültige Tagesreihenfolge")
    try:
        db.execute(
            "UPDATE gym_plan_days SET name=%s,sort_order=%s WHERE id=%s",
            (new_name, new_order, day_id),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Dieser Tagesname existiert bereits") from None
        raise
    return next(day for day in get_plan(int(day["plan_id"]))["days"] if day["id"] == int(day_id))


def delete_day(day_id: int) -> None:
    if db.execute("DELETE FROM gym_plan_days WHERE id=%s", (day_id,)) == 0:
        raise TrackerError(404, "Plantag nicht gefunden")


def reorder_days(plan_id: int, day_ids: list[int]) -> dict:
    plan = get_plan(plan_id)
    current = [day["id"] for day in plan["days"]]
    if len(day_ids) != len(current) or set(day_ids) != set(current):
        raise TrackerError(400, "Die Tagesreihenfolge muss alle Plantage genau einmal enthalten")
    with db.cursor() as cur:
        for order, day_id in enumerate(day_ids):
            cur.execute("UPDATE gym_plan_days SET sort_order=%s WHERE id=%s", (order, day_id))
    return get_plan(plan_id)


def _validate_plan_exercise_values(
    target_sets, rep_min, rep_max, rest_s, target_unit: str = "reps",
) -> tuple[int, int, int, int]:
    values = (target_sets, rep_min, rep_max, rest_s)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TrackerError(400, "Sätze, Wiederholungsbereich und Pausenzeit müssen ganze Zahlen sein")
    if not 1 <= target_sets <= 20 or not 1 <= rep_min <= rep_max <= (MAX_SECONDS if target_unit == "seconds" else MAX_REPS) or not 0 <= rest_s <= 900:
        raise TrackerError(400, "Sätze, Wiederholungsbereich oder Pausenzeit liegen außerhalb des gültigen Bereichs")
    return target_sets, rep_min, rep_max, rest_s


def add_exercise(
    day_id: int, exercise_id: int, *, target_sets: int = 3,
    rep_min: int | None = None, rep_max: int | None = None,
    rest_s: int = DEFAULT_REST_SECONDS,
) -> dict:
    day = db.query_one("SELECT id FROM gym_plan_days WHERE id=%s", (day_id,))
    if not day:
        raise TrackerError(404, "Plantag nicht gefunden")
    exercise = db.query_one("SELECT id,rep_min,rep_max,unit FROM exercises WHERE id=%s", (exercise_id,))
    if not exercise:
        raise TrackerError(404, "Übung nicht gefunden")
    target_unit = "seconds" if str(exercise.get("unit") or "").casefold() == "min" else "reps"
    if target_unit == "seconds":
        low = 30 if rep_min is None else rep_min
        high = low + SECONDS_RANGE_WIDTH if rep_max is None else rep_max
    else:
        low = int(exercise.get("rep_min") or 6) if rep_min is None else rep_min
        high = int(exercise.get("rep_max") or 10) if rep_max is None else rep_max
    target_sets, low, high, rest_s = _validate_plan_exercise_values(
        target_sets, low, high, rest_s, target_unit,
    )
    order = db.query_one("SELECT COALESCE(MAX(sort_order),-1)+1 AS next FROM gym_plan_exercises WHERE day_id=%s", (day_id,))["next"]
    try:
        item_id = db.insert_returning(
            """INSERT INTO gym_plan_exercises
                 (day_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (day_id, exercise_id, order, target_sets, low, high, rest_s, target_unit),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Die Übung ist bereits an diesem Tag eingetragen") from None
        raise
    return db.query_one(
        """SELECT pe.id,pe.exercise_id,e.name,e.muscle,e.equipment,pe.sort_order,
                  pe.target_sets,pe.rep_min,pe.rep_max,pe.rest_s,pe.target_unit
           FROM gym_plan_exercises pe JOIN exercises e ON e.id=pe.exercise_id WHERE pe.id=%s""",
        (item_id,),
    )


def update_plan_exercise(entry_id: int, **values) -> dict:
    allowed = {"sort_order", "target_sets", "rep_min", "rep_max", "rest_s"}
    if not values or values.keys() - allowed:
        raise TrackerError(400, "Ungültige Planübung-Änderung")
    item = db.query_one(
        "SELECT id,day_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit FROM gym_plan_exercises WHERE id=%s",
        (entry_id,),
    )
    if not item:
        raise TrackerError(404, "Planübung nicht gefunden")
    updated = {key: int(item[key]) for key in ("sort_order", "target_sets", "rep_min", "rep_max", "rest_s")}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TrackerError(400, "Planwerte müssen ganze Zahlen sein")
        updated[key] = value
    if updated["sort_order"] < 0:
        raise TrackerError(400, "Ungültige Übungsreihenfolge")
    _validate_plan_exercise_values(
        updated["target_sets"], updated["rep_min"], updated["rep_max"], updated["rest_s"],
        item["target_unit"],
    )
    if "sort_order" in values:
        rows = db.query(
            "SELECT id,sort_order FROM gym_plan_exercises WHERE day_id=%s ORDER BY sort_order,id",
            (item["day_id"],),
        )
        ordered_ids = [int(row["id"]) for row in rows]
        if updated["sort_order"] >= len(ordered_ids):
            raise TrackerError(400, "Ungültige Übungsreihenfolge")
        ordered_ids.remove(entry_id)
        ordered_ids.insert(updated["sort_order"], entry_id)
        offset = max((int(row["sort_order"]) for row in rows), default=-1) + len(rows) + 1
        with db.cursor() as cur:
            cur.execute(
                "UPDATE gym_plan_exercises SET sort_order=sort_order+%s WHERE day_id=%s",
                (offset, item["day_id"]),
            )
            for sort_order, moved_id in enumerate(ordered_ids):
                cur.execute(
                    "UPDATE gym_plan_exercises SET sort_order=%s WHERE id=%s",
                    (sort_order, moved_id),
                )
    db.execute(
        """UPDATE gym_plan_exercises SET target_sets=%s,rep_min=%s,rep_max=%s,rest_s=%s
           WHERE id=%s""",
        (updated["target_sets"], updated["rep_min"], updated["rep_max"],
         updated["rest_s"], entry_id),
    )
    return db.query_one(
        """SELECT pe.id,pe.exercise_id,e.name,e.muscle,e.equipment,pe.sort_order,
                  pe.target_sets,pe.rep_min,pe.rep_max,pe.rest_s,pe.target_unit
           FROM gym_plan_exercises pe JOIN exercises e ON e.id=pe.exercise_id WHERE pe.id=%s""",
        (entry_id,),
    )


def delete_plan_exercise(entry_id: int) -> None:
    if db.execute("DELETE FROM gym_plan_exercises WHERE id=%s", (entry_id,)) == 0:
        raise TrackerError(404, "Planübung nicht gefunden")


def reorder_exercises(day_id: int, entry_ids: list[int]) -> dict:
    if not db.query_one("SELECT id FROM gym_plan_days WHERE id=%s", (day_id,)):
        raise TrackerError(404, "Plantag nicht gefunden")
    rows = db.query("SELECT id FROM gym_plan_exercises WHERE day_id=%s", (day_id,))
    current = {int(row["id"]) for row in rows}
    if len(entry_ids) != len(current) or set(entry_ids) != current:
        raise TrackerError(400, "Die Übungsreihenfolge muss alle Planübungen genau einmal enthalten")
    with db.cursor() as cur:
        for order, entry_id in enumerate(entry_ids):
            cur.execute("UPDATE gym_plan_exercises SET sort_order=%s WHERE id=%s", (order, entry_id))
    day = db.query_one("SELECT plan_id FROM gym_plan_days WHERE id=%s", (day_id,))
    plan = get_plan(int(day["plan_id"]))
    return next(item for item in plan["days"] if item["id"] == day_id)


def _week_days(today: date) -> list[dict]:
    monday = today - timedelta(days=today.weekday())
    rows = db.query(
        """SELECT w.date FROM workouts w WHERE w.type IN ('gym','strength')
           AND w.date >= %s AND w.date <= %s
           AND EXISTS (SELECT 1 FROM workout_sets ws WHERE ws.workout_id=w.id
                       AND COALESCE(ws.is_warmup,FALSE)=FALSE)
           GROUP BY w.date""",
        (monday, monday + timedelta(days=6)),
    )
    training_dates = {_date_value(row["date"]) for row in rows}
    return [
        {
            "date": (monday + timedelta(days=offset)).isoformat(),
            "label": (monday + timedelta(days=offset)).strftime("%a"),
            "trained": monday + timedelta(days=offset) in training_dates,
            "today": monday + timedelta(days=offset) == today,
        }
        for offset in range(7)
    ]


def _plan_workouts(start_date, plan_name: str) -> list[dict]:
    """Nur Workouts dieses Plans (AP-Titel endet auf '· <Planname>')."""
    return db.query(
        """SELECT w.id,w.date,w.title,w.created_at FROM workouts w
           WHERE w.type IN ('gym','strength')
             AND w.date >= %s
             AND lower(regexp_replace(w.title, '^.*·\\s*', '')) = lower(%s)
             AND EXISTS (SELECT 1 FROM workout_sets ws WHERE ws.workout_id=w.id
                         AND COALESCE(ws.is_warmup,FALSE)=FALSE)
           ORDER BY w.date DESC,w.created_at DESC,w.id DESC""",
        (start_date, plan_name.strip()),
    )


def home_state(now: datetime | date | None = None) -> dict:
    today = now.date() if isinstance(now, datetime) else now or date.today()
    plan = active_plan()
    if not plan:
        return {"active_plan": None, "week_number": None, "week_days": _week_days(today), "next_day": None}
    days = plan["days"]
    rows = _plan_workouts(plan["start_date"], plan["name"])
    last_day, next_day = _rotation_next_day(days, rows)
    return {
        "active_plan": plan,
        "week_number": rotation_week_number(plan["start_date"], days, rows),
        "week_days": _week_days(today),
        "last_day": last_day,
        "next_day": next_day,
    }


def _exercise_ids(exercise_id: int) -> list[int]:
    exercise = db.query_one("SELECT id,name FROM exercises WHERE id=%s", (exercise_id,))
    if not exercise:
        return []
    aliases = db.query("SELECT alias FROM exercise_aliases WHERE exercise_id=%s", (exercise_id,))
    names = {normalize_alias(exercise["name"]), *(normalize_alias(row["alias"]) for row in aliases)}
    ids = [int(row["id"]) for row in db.query("SELECT id,name FROM exercises")
           if normalize_alias(row["name"]) in names]
    return sorted(set(ids) | {int(exercise_id)})


def _last_sets(exercise_id: int, *, exclude_workout_id: int | None = None) -> list[dict]:
    ids = _exercise_ids(exercise_id)
    if not ids:
        return []
    query = """SELECT w.id AS workout_id FROM workouts w JOIN workout_sets ws ON ws.workout_id=w.id
                WHERE w.type IN ('gym','strength') AND ws.exercise_id=ANY(%s)
                  AND COALESCE(ws.is_warmup,FALSE)=FALSE"""
    params: list = [ids]
    if exclude_workout_id is not None:
        query += " AND w.id<>%s"
        params.append(exclude_workout_id)
    query += " GROUP BY w.id,w.date,w.created_at ORDER BY w.date DESC,w.created_at DESC,w.id DESC LIMIT 1"
    recent = db.query_one(query, tuple(params))
    if not recent:
        return []
    return db.query(
        """SELECT ws.weight_kg,ws.reps,ws.duration_s,ws.set_index FROM workout_sets ws
           WHERE ws.workout_id=%s AND ws.exercise_id=ANY(%s)
             AND COALESCE(ws.is_warmup,FALSE)=FALSE ORDER BY ws.set_index,ws.id""",
        (recent["workout_id"], ids),
    )


def _set_amount(row: dict) -> int:
    """Wdh. eines Satzes; bei zeitbasierten Sätzen ohne Wdh. die Dauer in Sekunden."""
    value = row.get("reps")
    if value is None:
        value = row.get("duration_s")
    return int(value or 0)


def _session_entry_state(entry: dict, workout_id: int) -> dict:
    ids = _exercise_ids(int(entry["exercise_id"]))
    logged = db.query(
        """SELECT id,client_id,weight_kg,reps,duration_s,set_index FROM workout_sets
           WHERE workout_id=%s AND exercise_id=ANY(%s) AND COALESCE(is_warmup,FALSE)=FALSE
           ORDER BY set_index,id""",
        (workout_id, ids),
    ) if ids else []
    return {
        "id": int(entry["exercise_id"]),
        "exercise_id": int(entry["exercise_id"]),
        "name": entry["name"],
        "muscle": entry.get("muscle"),
        "equipment": entry.get("equipment"),
        "rest_s": int(entry.get("rest_s") or DEFAULT_REST_SECONDS),
        "target_sets": int(entry.get("target_sets") or 3),
        "rep_min": int(entry.get("rep_min") or 6),
        "rep_max": int(entry.get("rep_max") or 10),
        "target_unit": entry.get("target_unit") or "reps",
        "sort_order": int(entry.get("sort_order") or 0),
        "session_exercise_id": int(entry.get("session_exercise_id") or 0),
        "note": entry.get("note") or "",
        "skipped": bool(entry.get("skipped")),
        "prefill": [
            {"weight_kg": float(row["weight_kg"]) if row.get("weight_kg") is not None else None,
             "reps": _set_amount(row)}
            for row in _last_sets(int(entry["exercise_id"]), exclude_workout_id=workout_id)
        ],
        "logged": [
            {"id": int(row["id"]), "client_id": row.get("client_id"),
             "weight_kg": float(row["weight_kg"]) if row.get("weight_kg") is not None else None,
             "reps": _set_amount(row), "set_index": int(row["set_index"])}
            for row in logged
        ],
    }


def _snapshot_day(cur, workout_id: int, day_id: int) -> None:
    cur.execute(
        """INSERT INTO gym_workout_exercises
             (workout_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit)
           SELECT %s,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit
           FROM gym_plan_exercises WHERE day_id=%s
           ON CONFLICT (workout_id,exercise_id) DO NOTHING""",
        (workout_id, day_id),
    )


def session_exercises(workout_id: int, title: str | None, fallback: list[dict] | None = None) -> list[dict]:
    rows = db.query(
        """SELECT we.id AS session_exercise_id,we.exercise_id,we.sort_order,we.target_sets,
                  we.rep_min,we.rep_max,we.rest_s,we.target_unit,we.note,we.skipped,we.removed,
                  e.name,e.muscle,e.equipment
           FROM gym_workout_exercises we JOIN exercises e ON e.id=we.exercise_id
           WHERE we.workout_id=%s ORDER BY we.sort_order,we.id""",
        (workout_id,),
    )
    if not rows:
        day_name = _day_from_title(title)
        active = db.query_one("SELECT id FROM gym_plans WHERE active=TRUE") if day_name else None
        day = db.query_one(
            "SELECT id FROM gym_plan_days WHERE plan_id=%s AND name=%s", (active["id"], day_name)
        ) if active else None
        if day:
            with db.cursor() as cur:
                _snapshot_day(cur, workout_id, int(day["id"]))
            return session_exercises(workout_id, title, fallback)
    if rows:
        return [_session_entry_state(row, workout_id) for row in rows if not row.get("removed")]
    result = []
    for index, exercise in enumerate(fallback or []):
        row = db.query_one("SELECT id,muscle,equipment,rep_min,rep_max FROM exercises WHERE LOWER(name)=LOWER(%s) LIMIT 1", (exercise["name"],))
        if not row:
            continue
        entry = {
            **exercise,
            "exercise_id": int(row["id"]),
            "name": exercise["name"],
            "muscle": row.get("muscle"),
            "equipment": row.get("equipment"),
            "sort_order": index,
            "target_sets": max(1, len(exercise.get("prefill") or [])),
            "rep_min": int(row.get("rep_min") or 6),
            "rep_max": int(row.get("rep_max") or 10),
            "rest_s": exercise.get("rest_s") or DEFAULT_REST_SECONDS,
            "session_exercise_id": 0,
            "note": "",
            "skipped": False,
        }
        result.append(_session_entry_state(entry, workout_id))
    return result


def _open_workout(workout_id: int) -> dict:
    row = db.query_one(
        "SELECT id FROM workouts WHERE id=%s AND type='gym' AND ended_at IS NULL",
        (workout_id,),
    )
    if not row:
        raise TrackerError(409, "Training ist nicht offen")
    return row


def add_session_exercise(workout_id: int, exercise_id: int) -> dict:
    _open_workout(workout_id)
    exercise = db.query_one(
        "SELECT id,name,muscle,equipment,unit,rep_min,rep_max FROM exercises WHERE id=%s",
        (exercise_id,),
    )
    if not exercise:
        raise TrackerError(404, "Übung nicht gefunden")
    existing = db.query_one(
        "SELECT id,removed FROM gym_workout_exercises WHERE workout_id=%s AND exercise_id=%s",
        (workout_id, exercise_id),
    )
    if existing and not existing.get("removed"):
        raise TrackerError(409, "Die Übung ist bereits im Training")
    candidate_ids = set(_exercise_ids(exercise_id))
    current_entries = db.query(
        "SELECT exercise_id FROM gym_workout_exercises WHERE workout_id=%s AND removed=FALSE",
        (workout_id,),
    )
    if any(candidate_ids.intersection(_exercise_ids(int(item["exercise_id"]))) for item in current_entries):
        raise TrackerError(409, "Die Übung ist bereits im Training")
    target_unit = "seconds" if exercise.get("unit") == "min" else "reps"
    next_order = db.query_one(
        "SELECT COALESCE(MAX(sort_order),-1)+1 AS next FROM gym_workout_exercises WHERE workout_id=%s AND removed=FALSE",
        (workout_id,),
    )["next"]
    if target_unit == "seconds":
        # Wdh.-Defaults der Übung passen nicht zu Haltezeiten.
        low, high = 30, 40
    else:
        low = int(exercise.get("rep_min") or 6)
        high = max(low, int(exercise.get("rep_max") or 10))
    if existing:
        db.execute(
            """UPDATE gym_workout_exercises
               SET sort_order=%s,target_sets=3,rep_min=%s,rep_max=%s,rest_s=%s,target_unit=%s,
                   note='',skipped=FALSE,removed=FALSE
               WHERE id=%s""",
            (next_order, low, high, DEFAULT_REST_SECONDS, target_unit, existing["id"]),
        )
        row = db.query_one(
            """SELECT we.id AS session_exercise_id,we.exercise_id,we.sort_order,we.target_sets,
                      we.rep_min,we.rep_max,we.rest_s,we.target_unit,we.note,we.skipped,
                      e.name,e.muscle,e.equipment
               FROM gym_workout_exercises we JOIN exercises e ON e.id=we.exercise_id WHERE we.id=%s""",
            (existing["id"],),
        )
        return _session_entry_state(row, workout_id)
    try:
        new_id = db.insert_returning(
            """INSERT INTO gym_workout_exercises
                 (workout_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s,target_unit)
               VALUES (%s,%s,%s,3,%s,%s,%s,%s) RETURNING id""",
            (workout_id, exercise_id, next_order, low, high, DEFAULT_REST_SECONDS, target_unit),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Die Übung ist bereits im Training") from None
        raise
    entry = {
        **exercise, "exercise_id": exercise_id, "sort_order": next_order,
        "target_sets": 3, "rep_min": low, "rep_max": high, "target_unit": target_unit,
        "rest_s": DEFAULT_REST_SECONDS, "session_exercise_id": int(new_id),
    }
    return _session_entry_state(entry, workout_id)


def update_session_exercise(workout_id: int, session_exercise_id: int, **values) -> dict:
    allowed = {"sort_order", "note", "skipped", "exercise_id", "target_sets", "rep_min", "rep_max", "rest_s"}
    if not values or values.keys() - allowed:
        raise TrackerError(400, "Ungültige Trainingsübung-Änderung")
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM workouts WHERE id=%s AND type='gym' AND ended_at IS NULL FOR UPDATE",
            (workout_id,),
        )
        if not cur.fetchone():
            raise TrackerError(409, "Training ist nicht offen")
        cur.execute(
            """SELECT id,exercise_id,sort_order,note,skipped,target_sets,rep_min,rep_max,rest_s,target_unit
               FROM gym_workout_exercises
               WHERE workout_id=%s AND id=%s AND removed=FALSE FOR UPDATE""",
            (workout_id, session_exercise_id),
        )
        entry = cur.fetchone()
        if not entry:
            raise TrackerError(404, "Trainingsübung nicht gefunden")
        entry = dict(entry)
        order = values.get("sort_order", int(entry["sort_order"]))
        note = values.get("note", entry.get("note") or "")
        skipped = values.get("skipped", bool(entry.get("skipped")))
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise TrackerError(400, "Ungültige Übungsreihenfolge")
        if not isinstance(note, str) or len(note) > 1000 or not isinstance(skipped, bool):
            raise TrackerError(400, "Notiz oder Status ist ungültig")
        exercise_id = values.get("exercise_id", int(entry["exercise_id"]))
        if isinstance(exercise_id, bool) or not isinstance(exercise_id, int):
            raise TrackerError(400, "Ungültige Übung")
        replacing = exercise_id != int(entry["exercise_id"])
        cur.execute(
            "SELECT id,name,muscle,equipment,unit,rep_min,rep_max FROM exercises WHERE id=%s",
            (exercise_id,),
        )
        exercise = cur.fetchone()
        if not exercise:
            raise TrackerError(404, "Übung nicht gefunden")
        exercise = dict(exercise)
        if replacing:
            old_ids = _exercise_ids(int(entry["exercise_id"]))
            cur.execute(
                """SELECT id FROM workout_sets WHERE workout_id=%s
                   AND exercise_id=ANY(%s) LIMIT 1""",
                (workout_id, old_ids),
            )
            if old_ids and cur.fetchone():
                raise TrackerError(409, "Eine Übung mit gespeicherten Sätzen kann nicht ersetzt werden")
            candidate_ids = set(_exercise_ids(exercise_id))
            cur.execute(
                """SELECT id,exercise_id FROM gym_workout_exercises
                   WHERE workout_id=%s AND id<>%s AND removed=FALSE""",
                (workout_id, session_exercise_id),
            )
            existing_entries = [dict(row) for row in cur.fetchall()]
            if any(candidate_ids.intersection(_exercise_ids(int(item["exercise_id"]))) for item in existing_entries):
                raise TrackerError(409, "Die Übung ist bereits im Training")
        target_unit = "seconds" if str(exercise.get("unit") or "").casefold() == "min" else "reps"
        if replacing:
            low = 30 if target_unit == "seconds" else int(exercise.get("rep_min") or 6)
            high = low + SECONDS_RANGE_WIDTH if target_unit == "seconds" else max(low, int(exercise.get("rep_max") or 10))
        else:
            low = int(entry.get("rep_min") or 6)
            high = int(entry.get("rep_max") or max(low, int(exercise.get("rep_max") or 10)))
        target_sets = values.get("target_sets", int(entry.get("target_sets") or 3))
        low = values.get("rep_min", low)
        high = values.get("rep_max", high)
        rest_s = values.get("rest_s", int(entry.get("rest_s") or DEFAULT_REST_SECONDS))
        target_sets, low, high, rest_s = _validate_plan_exercise_values(
            target_sets, low, high, rest_s, target_unit,
        )
        if replacing:
            cur.execute(
                "DELETE FROM gym_workout_exercises WHERE workout_id=%s AND exercise_id=%s AND removed=TRUE",
                (workout_id, exercise_id),
            )
        if "sort_order" in values:
            cur.execute(
                """SELECT id,sort_order FROM gym_workout_exercises
                   WHERE workout_id=%s AND removed=FALSE ORDER BY sort_order,id""",
                (workout_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            ordered_ids = [int(row["id"]) for row in rows]
            if order >= len(ordered_ids):
                raise TrackerError(400, "Ungültige Übungsreihenfolge")
            ordered_ids.remove(session_exercise_id)
            ordered_ids.insert(order, session_exercise_id)
            offset = max((int(row["sort_order"]) for row in rows), default=-1) + len(rows) + 1
            cur.execute(
                "UPDATE gym_workout_exercises SET sort_order=sort_order+%s WHERE workout_id=%s AND removed=FALSE",
                (offset, workout_id),
            )
            for sort_order, entry_id in enumerate(ordered_ids):
                cur.execute(
                    "UPDATE gym_workout_exercises SET sort_order=%s WHERE id=%s",
                    (sort_order, entry_id),
                )
        cur.execute(
            """UPDATE gym_workout_exercises
               SET exercise_id=%s,target_sets=%s,rep_min=%s,rep_max=%s,rest_s=%s,target_unit=%s,
                   note=%s,skipped=%s WHERE id=%s""",
            (exercise_id, target_sets, low, high, rest_s, target_unit, note, skipped, session_exercise_id),
        )
        cur.execute(
            """SELECT we.id AS session_exercise_id,we.exercise_id,we.sort_order,we.target_sets,
                      we.rep_min,we.rep_max,we.rest_s,we.note,we.skipped,e.name,e.muscle,e.equipment
               FROM gym_workout_exercises we JOIN exercises e ON e.id=we.exercise_id WHERE we.id=%s""",
            (session_exercise_id,),
        )
        exercise = dict(cur.fetchone())
    return _session_entry_state(exercise, workout_id)


def remove_session_exercise(workout_id: int, session_exercise_id: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM workouts WHERE id=%s AND type='gym' AND ended_at IS NULL FOR UPDATE",
            (workout_id,),
        )
        if not cur.fetchone():
            raise TrackerError(409, "Training ist nicht offen")
        cur.execute(
            """SELECT id,exercise_id FROM gym_workout_exercises
               WHERE workout_id=%s AND id=%s AND removed=FALSE FOR UPDATE""",
            (workout_id, session_exercise_id),
        )
        entry = cur.fetchone()
        if not entry:
            raise TrackerError(404, "Trainingsübung nicht gefunden")
        ids = _exercise_ids(int(entry["exercise_id"]))
        if ids:
            cur.execute(
                """SELECT id FROM workout_sets WHERE workout_id=%s
                   AND exercise_id=ANY(%s) LIMIT 1""",
                (workout_id, ids),
            )
            if cur.fetchone():
                raise TrackerError(409, "Eine Übung mit gespeicherten Sätzen kann nicht entfernt werden")
        cur.execute(
            "UPDATE gym_workout_exercises SET removed=TRUE WHERE workout_id=%s AND id=%s",
            (workout_id, session_exercise_id),
        )
        cur.execute(
            "SELECT id FROM gym_workout_exercises WHERE workout_id=%s AND removed=FALSE ORDER BY sort_order,id",
            (workout_id,),
        )
        rows = cur.fetchall()
        for sort_order, row in enumerate(rows):
            cur.execute("UPDATE gym_workout_exercises SET sort_order=%s WHERE id=%s", (sort_order, row["id"]))


def start_plan_session(day_id: int, now: datetime | None = None) -> dict:
    day = db.query_one(
        """SELECT d.id,d.name,p.id AS plan_id,p.name AS plan_name,p.start_date,p.active
           FROM gym_plan_days d JOIN gym_plans p ON p.id=d.plan_id WHERE d.id=%s""",
        (day_id,),
    )
    if not day:
        raise TrackerError(404, "Plantag nicht gefunden")
    if not day["active"]:
        raise TrackerError(409, "Nur Plantage des aktiven Plans können gestartet werden")
    moment = session.local_now(now)
    current = session.open_session(moment)
    if current:
        raise TrackerError(409, "Es läuft bereits ein Training")
    plan_days = db.query(
        "SELECT id,name,sort_order FROM gym_plan_days WHERE plan_id=%s ORDER BY sort_order,id",
        (day["plan_id"],),
    )
    number = rotation_week_number(day["start_date"], plan_days, _plan_workouts(day["start_date"], day["plan_name"]))
    day_number = next(
        index for index, plan_day in enumerate(plan_days, 1)
        if int(plan_day["id"]) == int(day_id)
    )
    title = f"{day['name']} · Day {day_number} · Week {number} · {day['plan_name']}"
    workout = session.start_session(title, now=moment)
    with db.cursor() as cur:
        _snapshot_day(cur, int(workout["id"]), day_id)
    return {
        "id": int(workout["id"]),
        "date": _date_value(workout["date"]).isoformat(),
        "started_at": workout["created_at"].isoformat() if isinstance(workout["created_at"], datetime) else str(workout["created_at"]),
        "title": title,
        "day_id": int(day_id),
        "week_number": number,
    }


def start_free_session(now: datetime | None = None) -> dict:
    """Startet ein freies Training mit den drei bisherigen Startübungen."""
    moment = session.local_now(now)
    if session.open_session(moment):
        raise TrackerError(409, "Es läuft bereits ein Training")
    workout = session.start_session("Freies Training", now=moment)
    with db.cursor() as cur:
        for sort_order, name in enumerate(EXERCISES):
            cur.execute(
                """SELECT id,rep_min,rep_max FROM exercises WHERE LOWER(name)=LOWER(%s) LIMIT 1""",
                (name,),
            )
            exercise = cur.fetchone()
            if not exercise:
                continue
            low = int(exercise["rep_min"] or 6)
            high = max(low, int(exercise["rep_max"] or 10))
            cur.execute(
                """INSERT INTO gym_workout_exercises
                     (workout_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s)
                   VALUES (%s,%s,%s,3,%s,%s,%s)
                   ON CONFLICT (workout_id,exercise_id) DO NOTHING""",
                (workout["id"], exercise["id"], sort_order, low, high, REST_SECONDS[name]),
            )
    return {
        "id": int(workout["id"]),
        "date": _date_value(workout["date"]).isoformat(),
        "started_at": workout["created_at"].isoformat() if isinstance(workout["created_at"], datetime) else str(workout["created_at"]),
        "title": workout["title"],
    }
