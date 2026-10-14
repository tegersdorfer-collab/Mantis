"""Transaktionale Gym-Sessionen und gezielte Satzkorrekturen."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

from core import db
from domains import fitness
from domains.gym.aliases import is_pullup_name, normalize_alias
from domains.gym.progression import personal_records, progression_set_count, recommend_next


LOCAL_TZ = ZoneInfo("Europe/Berlin")
SESSION_TIMEOUT = timedelta(hours=2)
_GYM_LOCK_ID = 7061492


def local_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=LOCAL_TZ)
    return now.astimezone(LOCAL_TZ)


def session_timeout_end(last_activity_at: datetime) -> datetime:
    return last_activity_at + SESSION_TIMEOUT


def _duration_minutes(started_at: datetime | None, last_activity_at: datetime | None) -> int:
    if not started_at or not last_activity_at:
        return 0
    return max(0, int((last_activity_at - started_at).total_seconds() // 60))


def summarize_session(
    sets: list[dict], started_at: datetime | None, last_activity_at: datetime | None,
) -> dict:
    working = [row for row in sets if not row.get("is_warmup")]
    exercises = sorted({str(row.get("exercise")) for row in working if row.get("exercise")})
    volume = 0.0
    for row in working:
        try:
            weight = float(row.get("weight_kg"))
            reps = int(row.get("reps") or 0)
        except (TypeError, ValueError):
            continue
        if weight <= 0 or reps <= 0 or row.get("load_mode") == "bodyweight":
            continue
        multiplier = 2 if row.get("equipment") == "dumbbell" and row.get("dumbbell_pair", True) else 1
        volume += weight * reps * multiplier
    return {
        "duration_min": _duration_minutes(started_at, last_activity_at),
        "exercise_count": len(exercises),
        "set_count": len(working),
        "volume_kg": round(volume, 2),
        "exercises": exercises,
    }


def _fetchone(cur, sql: str, params=()):
    cur.execute(sql, params or None)
    row = cur.fetchone()
    return dict(row) if row else None


def _fetchall(cur, sql: str, params=()):
    cur.execute(sql, params or None)
    return [dict(row) for row in cur.fetchall()]


def _lock_session(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_GYM_LOCK_ID,))


def _open_row(cur):
    return _fetchone(
        cur,
        """SELECT id, date, created_at, last_activity_at, ended_at
           FROM workouts WHERE type='gym' AND ended_at IS NULL
           ORDER BY id DESC LIMIT 1 FOR UPDATE""",
    )


def _close_timed_out(cur, workout: dict) -> None:
    last = workout.get("last_activity_at") or workout.get("created_at")
    ended = session_timeout_end(last)
    cur.execute(
        "UPDATE workouts SET ended_at=%s, duration_min=%s WHERE id=%s AND ended_at IS NULL",
        (ended, _duration_minutes(workout.get("created_at"), last), workout["id"]),
    )


def _exercise_defaults(name: str, equipment: str | None, load_mode: str) -> dict:
    lower = name.casefold()
    default_equipment = {
        "bankdrücken": "barbell", "bankdruecken": "barbell", "bench press": "barbell",
        "kniebeuge": "barbell", "squat": "barbell", "squats": "barbell",
        "beinpresse": "machine", "leg press": "machine",
    }.get(lower)
    if is_pullup_name(name):
        load_mode = "added"
    equipment = equipment or default_equipment
    exercise_type = "isolation" if any(
        term in lower for term in (
            "curl", "fly", "flieg", "heben", "raise", "extension", "pushdown",
        )
    ) else "compound"
    low, high, start = (8, 12, 10) if exercise_type == "isolation" else (6, 10, 8)
    increment = {"barbell": 2.5, "dumbbell": 1.0, "machine": 2.5}.get(equipment)
    muscle = fitness.guess_muscle(name)
    return {
        "category": "strength", "muscle": muscle, "unit": "reps",
        "exercise_type": exercise_type, "equipment": equipment,
        "rep_min": low, "rep_max": high, "rep_start": start,
        "load_mode": load_mode, "weight_increment_kg": increment,
        "dumbbell_pair": equipment == "dumbbell",
    }


def _equipment_variant_name(cur, name: str, equipment: str | None, load_mode: str) -> str:
    """Gibt gerätegleiche Bestandsübungen zurück, sonst einen getrennten Namen."""
    if not equipment:
        return name
    label = {
        "dumbbell": "Kurzhantel",
        "barbell": "Langhantel",
        "ez_bar": "SZ-Stange",
        "machine": "Maschine",
    }.get(equipment)
    if not label:
        return name
    variant = f"{name} ({label})"
    rows = _fetchall(
        cur,
        """SELECT id,name,equipment,load_mode FROM exercises
           WHERE LOWER(name)=LOWER(%s) OR LOWER(name)=LOWER(%s)""",
        (name, variant),
    )
    for row in rows:
        effective_equipment = row.get("equipment") or _exercise_defaults(
            row["name"], None, row.get("load_mode") or load_mode
        )["equipment"]
        if effective_equipment == equipment:
            return row["name"]
    if any(normalize_alias(row["name"]) == normalize_alias(name) for row in rows):
        return variant
    return name


def _ensure_exercise(cur, name: str, alias: str | None, equipment: str | None,
                     load_mode: str, *, allow_create: bool) -> dict | None:
    storage_name = _equipment_variant_name(cur, name, equipment, load_mode)
    row = _fetchone(
        cur,
        """SELECT id, name, exercise_type, equipment, rep_min, rep_max, rep_start,
                  load_mode, weight_increment_kg, dumbbell_pair
           FROM exercises WHERE LOWER(name)=LOWER(%s) LIMIT 1""",
        (storage_name,),
    )
    if not row and alias and not equipment:
        row = _fetchone(
            cur,
            """SELECT e.id, e.name, e.exercise_type, e.equipment, e.rep_min, e.rep_max,
                      e.rep_start, e.load_mode, e.weight_increment_kg, e.dumbbell_pair
               FROM exercise_aliases ea JOIN exercises e ON e.id=ea.exercise_id
               WHERE ea.normalized_alias=%s LIMIT 1""",
            (normalize_alias(alias),),
        )
    if not row:
        if not allow_create:
            return None
        defaults = _exercise_defaults(name, equipment, load_mode)
        row = _fetchone(
            cur,
            """INSERT INTO exercises
                 (name, category, muscle, unit, exercise_type, equipment, rep_min,
                  rep_max, rep_start, load_mode, weight_increment_kg, dumbbell_pair)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name
               RETURNING id, name, exercise_type, equipment, rep_min, rep_max, rep_start,
                         load_mode, weight_increment_kg, dumbbell_pair""",
            (storage_name, defaults["category"], defaults["muscle"], defaults["unit"],
             defaults["exercise_type"], defaults["equipment"], defaults["rep_min"],
             defaults["rep_max"], defaults["rep_start"], defaults["load_mode"],
             defaults["weight_increment_kg"], defaults["dumbbell_pair"]),
        )
    else:
        defaults = _exercise_defaults(name, equipment, load_mode)
        row = _fetchone(
            cur,
            """UPDATE exercises SET
                 exercise_type=COALESCE(exercise_type,%s), equipment=COALESCE(equipment,%s),
                 rep_min=COALESCE(rep_min,%s), rep_max=COALESCE(rep_max,%s),
                 rep_start=COALESCE(rep_start,%s), load_mode=COALESCE(load_mode,%s),
                 weight_increment_kg=COALESCE(weight_increment_kg,%s),
                 dumbbell_pair=COALESCE(dumbbell_pair,%s)
               WHERE id=%s
               RETURNING id, name, exercise_type, equipment, rep_min, rep_max, rep_start,
                         load_mode, weight_increment_kg, dumbbell_pair""",
            (defaults["exercise_type"], defaults["equipment"], defaults["rep_min"],
             defaults["rep_max"], defaults["rep_start"], defaults["load_mode"],
             defaults["weight_increment_kg"], defaults["dumbbell_pair"], row["id"]),
        )
    if alias:
        cur.execute(
            """INSERT INTO exercise_aliases (alias, normalized_alias, exercise_id)
               VALUES (%s,%s,%s) ON CONFLICT (normalized_alias) DO NOTHING""",
            (alias, normalize_alias(alias), row["id"]),
        )
    return row


def _group_history(rows: list[dict]) -> list[list[dict]]:
    groups: dict[int, list[dict]] = defaultdict(list)
    order = []
    for row in rows:
        workout_id = row["workout_id"]
        if workout_id not in groups:
            order.append(workout_id)
        groups[workout_id].append(row)
    return [[row for row in groups[workout_id] if not row.get("is_warmup")]
            for workout_id in order]


def _planned_set_count(cur, exercise_name: str) -> int | None:
    rows = _fetchall(
        cur,
        "SELECT plan_json FROM training_plans WHERE active=TRUE ORDER BY created_at DESC LIMIT 1",
    )
    if not rows:
        return None
    plan = rows[0].get("plan_json")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            return None
    target = normalize_alias(exercise_name)

    def find(node):
        if isinstance(node, dict):
            name = normalize_alias(node.get("exercise") or node.get("name") or "")
            if name == target:
                sets = node.get("sets_target", node.get("sets"))
                if isinstance(sets, int) and 1 <= sets <= 20:
                    return sets
                if isinstance(sets, list) and 1 <= len(sets) <= 20:
                    return len(sets)
            for value in node.values():
                result = find(value)
                if result:
                    return result
        elif isinstance(node, list):
            for value in node:
                result = find(value)
                if result:
                    return result
        return None

    return find(plan)


def _exercise_rows(cur, exercise_id: int) -> list[dict]:
    return _fetchall(
        cur,
        """SELECT w.id AS workout_id, w.created_at, ws.set_index, ws.reps,
                  ws.weight_kg, COALESCE(ws.is_warmup,FALSE) AS is_warmup
           FROM workout_sets ws JOIN workouts w ON w.id=ws.workout_id
           WHERE ws.exercise_id=%s AND w.type IN ('strength','gym')
           ORDER BY w.created_at ASC, w.id ASC, ws.set_index ASC, ws.id ASC""",
        (exercise_id,),
    )


def _exercise_state(cur, workout_id: int, exercise_id: int) -> dict:
    groups = _group_history(_exercise_rows(cur, exercise_id))
    position = next(
        (index for index, group in enumerate(groups)
         if group and group[0]["workout_id"] == workout_id),
        None,
    )
    if position is None:
        return {}
    current = groups[position]
    previous = groups[position - 1] if position else []
    exercise = _fetchone(
        cur,
        """SELECT name, exercise_type, equipment, rep_min, rep_max, rep_start,
                  load_mode, weight_increment_kg, dumbbell_pair
           FROM exercises WHERE id=%s""",
        (exercise_id,),
    ) or {}
    metadata = {key: exercise.get(key) for key in (
        "exercise_type", "equipment", "rep_min", "rep_max", "rep_start",
        "load_mode", "weight_increment_kg", "dumbbell_pair",
    )}
    prior_rows = [row for group in groups if group and group[0]["workout_id"] != workout_id for row in group]
    count = progression_set_count(
        len(current), _planned_set_count(cur, exercise.get("name", "")),
        previous_count=len(previous),
    )
    return {
        "current_sets": current,
        "previous_sets": previous,
        "progression": recommend_next(current, previous, metadata, set_count=count),
        "records": personal_records(current, prior_rows, load_mode=metadata.get("load_mode") or "external"),
        "metadata": metadata,
    }


def add_sets(parsed, *, allow_create: bool = False, now: datetime | None = None) -> dict:
    """Legt Session und alle Sätze gemeinsam in einer Transaktion an."""
    moment = local_now(now)
    with db.cursor() as cur:
        _lock_session(cur)
        workout = _open_row(cur)
        if workout and moment >= session_timeout_end(workout.get("last_activity_at") or workout["created_at"]):
            _close_timed_out(cur, workout)
            workout = None
        if workout is None:
            workout = _fetchone(
                cur,
                """INSERT INTO workouts (date, title, type, last_activity_at)
                   VALUES (%s,'Gym','gym',%s) RETURNING id, date, created_at, last_activity_at, ended_at""",
                (moment.date(), moment),
            )
            if workout is None:
                raise RuntimeError("Gym-Workout konnte nicht angelegt werden")

        exercise = _ensure_exercise(
            cur, parsed.exercise, parsed.alias, parsed.equipment, parsed.load_mode,
            allow_create=allow_create or parsed.known,
        )
        if exercise is None:
            raise ValueError("Die neue Übung muss erst bestätigt werden")
        last = _fetchone(
            cur,
            "SELECT COALESCE(MAX(set_index),0) AS last_index FROM workout_sets WHERE workout_id=%s AND exercise_id=%s",
            (workout["id"], exercise["id"]),
        )
        next_index = int((last or {}).get("last_index") or 0)
        inserted_sets = []
        for item in parsed.sets:
            next_index += 1
            cur.execute(
                """INSERT INTO workout_sets
                     (workout_id, exercise_id, set_index, reps, weight_kg, is_warmup)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    workout["id"], exercise["id"], next_index, item.reps,
                    item.weight_kg, item.is_warmup,
                ),
            )
            inserted_sets.append({
                "set_index": next_index, "reps": item.reps,
                "weight_kg": item.weight_kg, "is_warmup": item.is_warmup,
            })
        cur.execute("UPDATE workouts SET last_activity_at=%s WHERE id=%s", (moment, workout["id"]))

        groups = _group_history(_exercise_rows(cur, exercise["id"]))
        current_position = next(
            (index for index, group in enumerate(groups)
             if group and group[0]["workout_id"] == workout["id"]),
            None,
        )
        if current_position is None:
            current = inserted_sets
            previous = groups[-1] if groups else []
        else:
            current = groups[current_position]
            previous = groups[current_position - 1] if current_position else []
        metadata = {key: exercise.get(key) for key in (
            "exercise_type", "equipment", "rep_min", "rep_max", "rep_start",
            "load_mode", "weight_increment_kg", "dumbbell_pair",
        )}
        if parsed.load_mode in {"added", "bodyweight"}:
            metadata["load_mode"] = (
                "added" if is_pullup_name(exercise["name"])
                else parsed.load_mode
            )
        prior_rows = [
            row for group in groups for row in group
            if row.get("workout_id") != workout["id"]
        ]
        set_count = progression_set_count(
            len(current), _planned_set_count(cur, exercise["name"]),
            previous_count=len(previous),
        )
        return {
            "workout_id": workout["id"], "date": workout["date"],
            "created_at": workout.get("created_at") or moment,
            "exercise": exercise["name"], "sets": inserted_sets,
            "current_sets": current, "previous_sets": previous,
            "records": personal_records(current, prior_rows, load_mode=metadata.get("load_mode") or "external"),
            "progression": recommend_next(current, previous, metadata, set_count=set_count),
            "metadata": metadata,
        }


def open_session(now: datetime | None = None) -> dict | None:
    """Liest eine offene Session und materialisiert den Timeout bei Bedarf."""
    moment = local_now(now)
    workout = db.query_one(
        """SELECT id, date, created_at, last_activity_at, ended_at
           FROM workouts WHERE type='gym' AND ended_at IS NULL ORDER BY id DESC LIMIT 1"""
    )
    if not workout:
        return None
    last = workout.get("last_activity_at") or workout.get("created_at")
    if moment >= session_timeout_end(last):
        with db.cursor() as cur:
            _lock_session(cur)
            locked = _open_row(cur)
            if locked and moment >= session_timeout_end(locked.get("last_activity_at") or locked["created_at"]):
                _close_timed_out(cur, locked)
        return None
    return workout


def finish(now: datetime | None = None) -> dict | None:
    """Schließt die offene Session oder materialisiert ihren Timeout atomar."""
    moment = local_now(now)
    with db.cursor() as cur:
        _lock_session(cur)
        workout = _open_row(cur)
        if not workout:
            return None
        last = workout.get("last_activity_at") or workout.get("created_at")
        timed_out = moment >= session_timeout_end(last)
        ended = session_timeout_end(last) if timed_out else moment
        sets = _fetchall(
            cur,
            """SELECT e.id AS exercise_id, e.name AS exercise, e.equipment, e.load_mode, e.dumbbell_pair,
                      ws.weight_kg, ws.reps, COALESCE(ws.is_warmup,FALSE) AS is_warmup
               FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s ORDER BY ws.id""",
            (workout["id"],),
        )
        summary = summarize_session(sets, workout.get("created_at"), last)
        prs = []
        for exercise_id in dict.fromkeys(row["exercise_id"] for row in sets):
            current_sets = [row for row in sets if row["exercise_id"] == exercise_id]
            historical = [row for row in _exercise_rows(cur, exercise_id)
                          if row["workout_id"] != workout["id"]]
            mode = current_sets[0].get("load_mode") or "external"
            if any(personal_records(current_sets, historical, load_mode=mode).values()):
                prs.append(current_sets[0]["exercise"])
        cur.execute(
            "UPDATE workouts SET ended_at=%s, duration_min=%s WHERE id=%s AND ended_at IS NULL",
            (ended, summary["duration_min"], workout["id"]),
        )
        return {"workout_id": workout["id"], "timed_out": timed_out, "prs": prs, **summary}


def correct_last_reps(
    reps: int,
    now: datetime | None = None,
    *,
    expected_reps: int | None = None,
) -> dict | None:
    moment = local_now(now)
    with db.cursor() as cur:
        _lock_session(cur)
        workout = _open_row(cur)
        if not workout or moment >= session_timeout_end(workout.get("last_activity_at") or workout["created_at"]):
            if workout:
                _close_timed_out(cur, workout)
            return None
        row = _fetchone(
            cur,
            """SELECT id, exercise_id, set_index, reps FROM workout_sets
               WHERE workout_id=%s ORDER BY id DESC LIMIT 1""",
            (workout["id"],),
        )
        if not row:
            return None
        if expected_reps is not None and row.get("reps") != expected_reps:
            return {"conflict": True, "reps": row.get("reps"), "expected_reps": expected_reps}
        cur.execute("UPDATE workout_sets SET reps=%s WHERE id=%s", (reps, row["id"]))
        cur.execute("UPDATE workouts SET last_activity_at=%s WHERE id=%s", (moment, workout["id"]))
        state = _exercise_state(cur, workout["id"], row["exercise_id"])
        return {**row, "reps": reps, "workout_id": workout["id"], **state}


def delete_last_set(now: datetime | None = None) -> dict | None:
    moment = local_now(now)
    with db.cursor() as cur:
        _lock_session(cur)
        workout = _open_row(cur)
        if not workout or moment >= session_timeout_end(workout.get("last_activity_at") or workout["created_at"]):
            if workout:
                _close_timed_out(cur, workout)
            return None
        row = _fetchone(
            cur,
            """SELECT ws.id, ws.exercise_id, ws.reps, ws.weight_kg, e.name AS exercise
               FROM workout_sets ws LEFT JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s ORDER BY ws.id DESC LIMIT 1""",
            (workout["id"],),
        )
        if not row:
            cur.execute("DELETE FROM workouts WHERE id=%s", (workout["id"],))
            return {"workout_id": workout["id"], "empty": True}
        cur.execute("DELETE FROM workout_sets WHERE id=%s", (row["id"],))
        remaining = _fetchone(cur, "SELECT COUNT(*) AS n FROM workout_sets WHERE workout_id=%s", (workout["id"],))
        if not remaining or not remaining["n"]:
            cur.execute("DELETE FROM workouts WHERE id=%s", (workout["id"],))
            empty = True
        else:
            cur.execute("UPDATE workouts SET last_activity_at=%s WHERE id=%s", (moment, workout["id"]))
            empty = False
        state = _exercise_state(cur, workout["id"], row["exercise_id"]) if not empty else {}
        return {**row, "workout_id": workout["id"], "empty": empty, **state}


def correct_last_load(exercise_name: str, weight_kg: float, now: datetime | None = None) -> dict | None:
    moment = local_now(now)
    with db.cursor() as cur:
        _lock_session(cur)
        workout = _open_row(cur)
        if not workout or moment >= session_timeout_end(workout.get("last_activity_at") or workout["created_at"]):
            if workout:
                _close_timed_out(cur, workout)
            return None
        row = _fetchone(
            cur,
            """SELECT ws.id, ws.exercise_id, ws.set_index, e.name AS exercise
               FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s AND LOWER(e.name)=LOWER(%s)
               ORDER BY ws.id DESC LIMIT 1""",
            (workout["id"], exercise_name),
        )
        if not row:
            return None
        cur.execute("UPDATE workout_sets SET weight_kg=%s WHERE id=%s", (weight_kg, row["id"]))
        cur.execute("UPDATE workouts SET last_activity_at=%s WHERE id=%s", (moment, workout["id"]))
        state = _exercise_state(cur, workout["id"], row["exercise_id"])
        return {**row, "weight_kg": weight_kg, "workout_id": workout["id"], **state}


def exercise_history(exercise_name: str, limit: int = 5) -> list[dict]:
    rows = db.query(
        """SELECT w.id AS workout_id, w.date, w.created_at, ws.set_index, ws.reps,
                  ws.weight_kg, COALESCE(ws.is_warmup,FALSE) AS is_warmup,
                  e.name AS exercise, e.load_mode
           FROM workout_sets ws JOIN workouts w ON w.id=ws.workout_id
           JOIN exercises e ON e.id=ws.exercise_id
           WHERE LOWER(e.name)=LOWER(%s) AND w.type IN ('strength','gym')
           ORDER BY w.created_at DESC, w.id DESC, ws.set_index ASC, ws.id ASC""",
        (exercise_name,),
    )
    grouped: dict[int, list[dict]] = defaultdict(list)
    order = []
    for row in rows:
        if row.get("is_warmup"):
            continue
        wid = row["workout_id"]
        if wid not in grouped:
            order.append(wid)
        grouped[wid].append(row)
    return [{"workout_id": wid, "date": grouped[wid][0].get("date"),
             "sets": grouped[wid]} for wid in order[:limit]]


def workout_exercise_history(limit: int = 12) -> list[dict]:
    return db.query(
        """SELECT w.id AS workout_id, w.date, w.created_at, e.name AS exercise,
                  e.muscle, ws.set_index, ws.reps, ws.weight_kg,
                  COALESCE(ws.is_warmup,FALSE) AS is_warmup
           FROM workouts w JOIN workout_sets ws ON ws.workout_id=w.id
           JOIN exercises e ON e.id=ws.exercise_id
           WHERE w.type IN ('strength','gym')
           ORDER BY w.created_at DESC, w.id DESC, ws.set_index ASC, ws.id ASC
           LIMIT %s""",
        (limit,),
    )

