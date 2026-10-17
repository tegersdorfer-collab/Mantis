"""Read-only mapping from Mantis' gym tables to the openGym state contract."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from core import db
from domains.gym import plans, tracker


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _milliseconds(value, *, fallback_date=None) -> int:
    if value is None and fallback_date is not None:
        value = datetime.combine(fallback_date, time.min, tzinfo=timezone.utc)
    if value is None:
        return 0
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _exercise_id(value) -> str:
    return f"m-{int(value)}"


def _routine_id(value) -> str:
    return f"r-{int(value)}"


def _plan_parts(active_plan):
    routines = []
    day_ids = {}
    for day in (active_plan or {}).get("days", []):
        day_id = day.get("id")
        if day_id is None:
            continue
        routine_id = _routine_id(day_id)
        day_name = str(day.get("name") or "")
        day_ids[day_name.casefold()] = routine_id
        routine_exercises = []
        for item in day.get("exercises", []):
            if item.get("exercise_id") is None:
                continue
            target_unit = str(item.get("target_unit") or "reps").casefold()
            target = {
                "id": _exercise_id(item["exercise_id"]),
                "sets": int(item.get("target_sets") or 0),
            }
            if target_unit == "seconds":
                target.update({"mode": "time", "sec": int(item.get("rep_max") or item.get("rep_min") or 0)})
            else:
                target.update({
                    "reps": int(item.get("rep_max") or 0),
                    "repsMin": int(item.get("rep_min") or 0),
                })
            routine_exercises.append(target)
        routines.append({
            "id": routine_id,
            "name": day_name,
            "emoji": "",
            "ex": routine_exercises,
        })
    return routines, day_ids


def _workout_name(title) -> str:
    name = str(title or "Workout").split("·", 1)[0].strip()
    return name or "Workout"


def _map_workout(workout, rows, exercise_by_id, day_ids):
    entries = []
    entries_by_id = {}
    volume = 0.0
    for row in rows:
        exercise_id = row.get("exercise_id")
        if exercise_id is None:
            continue
        entry_id = _exercise_id(exercise_id)
        entry = entries_by_id.get(entry_id)
        if entry is None:
            entry = {"id": entry_id, "sets": []}
            exercise = exercise_by_id.get(int(exercise_id), {})
            pair = exercise.get("dumbbell_pair")
            if exercise.get("equipment") == "dumbbell" and (pair is None or pair is True):
                entry["target"] = {"dbLoad": "each", "mantisPair": True}
            entries_by_id[entry_id] = entry
            entries.append(entry)

        weight = _number(row.get("weight_kg"))
        reps = row.get("reps")
        duration = row.get("duration_s")
        warmup = bool(row.get("is_warmup"))
        set_data = {"w": weight, "r": reps, "done": True}
        if duration is not None:
            set_data["sec"] = int(duration)
        if warmup:
            set_data["warmup"] = True
        entry["sets"].append(set_data)

        exercise = exercise_by_id.get(int(exercise_id), {})
        if not warmup and duration is None and weight is not None and reps is not None:
            pair = exercise.get("dumbbell_pair")
            multiplier = 2 if exercise.get("equipment") == "dumbbell" and (pair is None or pair is True) else 1
            volume += float(weight) * int(reps) * multiplier

    started = workout.get("created_at")
    finished = workout.get("ended_at")
    date_value = workout.get("date")
    title = workout.get("title")
    day_name = _workout_name(title)
    result = {
        "id": f"w-{int(workout['id'])}",
        "d": _iso(date_value),
        "start": _milliseconds(started, fallback_date=date_value),
        "end": _milliseconds(finished, fallback_date=date_value),
        "name": day_name,
        "entries": entries,
        "vol": round(volume, 2),
    }
    routine_id = day_ids.get(day_name.casefold())
    if routine_id:
        result["routineId"] = routine_id
    return result


def map_snapshot(
    *, workouts, workout_sets, exercises, active_plan, next_day, bodyweight,
    rest_sec, today,
) -> dict:
    """Map supplied DB rows; an open Workout-Session is deliberately omitted in phase 1."""
    exercise_by_id = {int(row["id"]): row for row in exercises if row.get("id") is not None}
    sets_by_workout = {}
    for row in workout_sets:
        sets_by_workout.setdefault(int(row["workout_id"]), []).append(row)

    routines, day_ids = _plan_parts(active_plan)
    mapped_workouts = [
        _map_workout(workout, sets_by_workout.get(int(workout["id"]), []), exercise_by_id, day_ids)
        for workout in workouts
    ]
    custom_exercises = [
        {
            "id": _exercise_id(row["id"]),
            "n": str(row.get("alias") or row.get("name") or ""),
            "bp": str(row.get("muscle") or ""),
            "tg": str(row.get("muscle") or ""),
            "eq": str(row.get("equipment") or ""),
            "cat": "cardio" if str(row.get("category") or "").casefold() == "cardio" else "strength",
            "custom": True,
        }
        for row in exercises
        if row.get("id") is not None
    ]
    latest_weight_by_day = {}
    for row in bodyweight:
        day = row.get("date")
        weight = _number(row.get("weight_kg"))
        if day is None or weight is None:
            continue
        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            continue
        if not 20 <= weight_value <= 500:
            continue
        source_order = int(row.get("source_order") or 0)
        rank = source_order
        key = _iso(day)
        previous = latest_weight_by_day.get(key)
        if previous is None or rank > previous[0]:
            latest_weight_by_day[key] = (rank, weight)
    weight_rows = [
        {"d": key, "w": value[1]}
        for key, value in sorted(latest_weight_by_day.items())
    ]
    day_plan = {}
    if next_day and next_day.get("id") is not None:
        day_plan[today.isoformat()] = _routine_id(next_day["id"])

    return {
        "workouts": mapped_workouts,
        "routines": routines,
        "dayPlan": day_plan,
        "week": {},
        "bodyweight": weight_rows,
        "customEx": custom_exercises,
        "unit": "kg",
        "restSec": int(rest_sec or 0),
    }


def get_state() -> dict:
    """Read the current gym data and map it to openGym's client state."""
    exercises = db.query(
        """SELECT e.id,e.name,e.muscle,e.equipment,e.category,e.unit,
                  e.dumbbell_pair,COALESCE(alias.alias,e.name) AS alias
           FROM exercises e
           LEFT JOIN LATERAL (
               SELECT ea.alias FROM exercise_aliases ea
               WHERE ea.exercise_id=e.id AND LOWER(ea.alias)<>LOWER(e.name)
               ORDER BY ea.id LIMIT 1
           ) alias ON TRUE
           ORDER BY e.id"""
    )
    workouts = db.query(
        """SELECT id,date,title,created_at,ended_at FROM workouts
           WHERE type IN ('gym','strength') AND ended_at IS NOT NULL
           ORDER BY date,created_at,id"""
    )
    workout_ids = [int(row["id"]) for row in workouts]
    workout_sets = db.query(
        """SELECT ws.workout_id,ws.exercise_id,ws.set_index,ws.weight_kg,ws.reps,
                  ws.duration_s,COALESCE(ws.is_warmup,FALSE) AS is_warmup
           FROM workout_sets ws
           WHERE ws.workout_id=ANY(%s)
           ORDER BY ws.workout_id,ws.set_index,ws.id""",
        (workout_ids,),
    ) if workout_ids else []
    plan = plans.active_plan()
    home = plans.home_state()
    bodyweight = db.query(
        """SELECT date,weight_kg,source_order FROM (
               SELECT date,weight_kg,0 AS source_order
               FROM body_measurements WHERE weight_kg BETWEEN 20 AND 500
               UNION ALL
               SELECT date,weight AS weight_kg,1 AS source_order
               FROM health_data WHERE weight BETWEEN 20 AND 500
           ) measurements
           ORDER BY date,source_order"""
    )
    default_rest = next(iter(tracker.REST_SECONDS.values()), plans.DEFAULT_REST_SECONDS)
    rest_sec = db.get_setting("gym_rest_sec", default_rest)
    try:
        rest_sec = int(rest_sec)
    except (TypeError, ValueError):
        rest_sec = int(default_rest)
    return map_snapshot(
        workouts=workouts,
        workout_sets=workout_sets,
        exercises=exercises,
        active_plan=plan,
        next_day=home.get("next_day"),
        bodyweight=bodyweight,
        rest_sec=rest_sec,
        today=date.today(),
    )


def get_revision() -> dict:
    row = db.query_one("SELECT rev,wid FROM gym_sync_state WHERE id=1")
    if not row:
        return {"rev": 0, "wid": ""}
    return {"rev": int(row["rev"]), "wid": str(row["wid"])}

