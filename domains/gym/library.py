"""Übungsbibliothek, e1RM-Verlauf und abgeschlossene Workouts."""

from __future__ import annotations

from datetime import date, datetime

from core import db
from domains.gym import plans, progression
from domains.gym.aliases import normalize_alias
from domains.gym.tracker import TrackerError, _reps, _weight


def _date_text(value) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)[:10] if value else None


def _exercise_row(exercise_id: int) -> dict:
    row = db.query_one(
        """SELECT id,name,category,muscle,unit,exercise_type,equipment,rep_min,
                  rep_max,weight_increment_kg,dumbbell_pair
           FROM exercises WHERE id=%s""",
        (exercise_id,),
    )
    if not row:
        raise TrackerError(404, "Übung nicht gefunden")
    return row


def list_exercises(query: str = "", muscle: str | None = None) -> list[dict]:
    rows = db.query(
        """SELECT id,name,category,muscle,unit,exercise_type,equipment,rep_min,rep_max
           FROM exercises WHERE COALESCE(category,'strength')<>'cardio'
             AND COALESCE(unit,'reps')='reps' ORDER BY LOWER(name),id"""
    )
    alias_rows = db.query(
        """SELECT ea.exercise_id,ea.alias FROM exercise_aliases ea
           WHERE ea.exercise_id IS NOT NULL"""
    )
    by_id: dict[int, list[str]] = {}
    for row in alias_rows:
        by_id.setdefault(int(row["exercise_id"]), []).append(row["alias"])
    needle = normalize_alias(query)
    muscle_key = normalize_alias(muscle or "")
    result = []
    for row in rows:
        aliases = list(dict.fromkeys(by_id.get(int(row["id"]), [])))
        haystack = normalize_alias(" ".join([row["name"], *aliases]))
        if needle and needle not in haystack:
            continue
        if muscle_key and normalize_alias(row.get("muscle") or "") != muscle_key:
            continue
        result.append({
            **row,
            "id": int(row["id"]),
            "rep_min": int(row["rep_min"] or 6),
            "rep_max": int(row["rep_max"] or 10),
            "aliases": aliases,
        })
    return result


def create_exercise(
    name: str, muscle: str | None, equipment: str | None,
    rep_min: int = 6, rep_max: int = 10,
) -> dict:
    clean_name = str(name or "").strip()
    clean_muscle = str(muscle or "").strip() or None
    clean_equipment = str(equipment or "").strip() or None
    if not clean_name or len(clean_name) > 100:
        raise TrackerError(400, "Der Übungsname muss 1 bis 100 Zeichen lang sein")
    if clean_muscle and len(clean_muscle) > 60:
        raise TrackerError(400, "Die Muskelgruppe darf höchstens 60 Zeichen lang sein")
    if clean_equipment not in {None, "barbell", "dumbbell", "machine", "cable", "bodyweight", "other"}:
        raise TrackerError(400, "Unbekanntes Gerät")
    if (
        isinstance(rep_min, bool) or isinstance(rep_max, bool)
        or not isinstance(rep_min, int) or not isinstance(rep_max, int)
        or not 1 <= rep_min <= rep_max <= 100
    ):
        raise TrackerError(400, "Der Wiederholungsbereich muss zwischen 1 und 100 liegen")
    duplicate = db.query_one("SELECT id FROM exercises WHERE LOWER(name)=LOWER(%s)", (clean_name,))
    if duplicate:
        raise TrackerError(409, "Eine Übung mit diesem Namen existiert bereits")
    try:
        exercise_id = db.insert_returning(
            """INSERT INTO exercises
                 (name,category,muscle,unit,exercise_type,equipment,rep_min,rep_max,rep_start)
               VALUES (%s,'strength',%s,'reps','compound',%s,%s,%s,%s) RETURNING id""",
            (clean_name, clean_muscle, clean_equipment, rep_min, rep_max,
             min(rep_max, max(rep_min, round((rep_min + rep_max) / 2)))),
        )
        db.execute(
            """INSERT INTO exercise_aliases (alias,normalized_alias,exercise_id)
               VALUES (%s,%s,%s) ON CONFLICT (normalized_alias) DO NOTHING""",
            (clean_name, normalize_alias(clean_name), exercise_id),
        )
    except Exception as error:
        if "unique" in str(error).lower():
            raise TrackerError(409, "Eine Übung mit diesem Namen existiert bereits") from None
        raise
    return _exercise_row(int(exercise_id))


def exercise_detail(exercise_id: int) -> dict:
    exercise = _exercise_row(exercise_id)
    ids = plans._exercise_ids(exercise_id)
    rows = db.query(
        """SELECT w.id AS workout_id,w.date,w.title,w.created_at,
                  ws.id AS set_id,ws.set_index,ws.weight_kg,ws.reps,ws.duration_s
           FROM workouts w JOIN workout_sets ws ON ws.workout_id=w.id
           WHERE w.type IN ('gym','strength') AND ws.exercise_id=ANY(%s)
             AND COALESCE(ws.is_warmup,FALSE)=FALSE
           ORDER BY w.date,w.created_at,w.id,ws.set_index,ws.id""",
        (ids,),
    ) if ids else []
    by_workout: dict[int, dict] = {}
    ordered = []
    for row in rows:
        workout_id = int(row["workout_id"])
        if workout_id not in by_workout:
            by_workout[workout_id] = {
                "workout_id": workout_id,
                "date": _date_text(row.get("date")),
                "title": row.get("title") or "Training",
                "sets": [],
                "e1rm_kg": None,
                "best_set": None,
            }
            ordered.append(by_workout[workout_id])
        weight = float(row["weight_kg"]) if row.get("weight_kg") is not None else None
        timed = progression.is_timed_set(row)
        reps = progression.set_amount(row)
        item = {"id": int(row["set_id"]), "set_index": int(row["set_index"]), "weight_kg": weight, "reps": reps}
        by_workout[workout_id]["sets"].append(item)
        estimate = None if timed else progression.estimate_1rm(weight, reps)
        if estimate is not None and (by_workout[workout_id]["e1rm_kg"] is None
                                     or estimate > by_workout[workout_id]["e1rm_kg"]):
            by_workout[workout_id]["e1rm_kg"] = round(estimate, 2)
            by_workout[workout_id]["best_set"] = item
    ordered.reverse()
    exercise["id"] = int(exercise["id"])
    exercise["rep_min"] = int(exercise["rep_min"] or 6)
    exercise["rep_max"] = int(exercise["rep_max"] or 10)
    exercise["history"] = ordered
    return exercise


def workout_list(limit: int = 50) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TrackerError(400, "limit muss zwischen 1 und 100 liegen")
    rows = db.query(
        """SELECT w.id,w.date,w.title,w.duration_min,w.created_at,w.ended_at,
                  COUNT(ws.id) FILTER (WHERE COALESCE(ws.is_warmup,FALSE)=FALSE) AS set_count,
                  COALESCE(SUM(CASE WHEN COALESCE(ws.is_warmup,FALSE)=FALSE
                                    AND COALESCE(e.load_mode,'external')<>'bodyweight'
                                    AND ws.weight_kg>0 AND ws.reps>0
                               THEN ws.weight_kg*ws.reps*
                                    CASE WHEN e.equipment='dumbbell'
                                              AND COALESCE(e.dumbbell_pair,TRUE)
                                         THEN 2 ELSE 1 END
                               ELSE 0 END),0) AS volume_kg
           FROM workouts w LEFT JOIN workout_sets ws ON ws.workout_id=w.id
           LEFT JOIN exercises e ON e.id=ws.exercise_id
           WHERE w.type IN ('gym','strength') AND w.ended_at IS NOT NULL
           GROUP BY w.id ORDER BY w.date DESC,w.created_at DESC,w.id DESC LIMIT %s""",
        (limit,),
    )
    return [
        {
            **row,
            "id": int(row["id"]),
            "date": _date_text(row.get("date")),
            "title": row.get("title") or "Training",
            "duration_min": int(row.get("duration_min") or 0),
            "set_count": int(row.get("set_count") or 0),
            "volume_kg": round(float(row.get("volume_kg") or 0), 2),
        }
        for row in rows
    ]


def workout_detail(workout_id: int) -> dict:
    workout = db.query_one(
        "SELECT id,date,title,duration_min,created_at,ended_at FROM workouts WHERE id=%s AND type IN ('gym','strength')",
        (workout_id,),
    )
    if not workout:
        raise TrackerError(404, "Training nicht gefunden")
    rows = db.query(
        """SELECT ws.id,ws.exercise_id,e.name AS exercise,e.muscle,e.equipment,
                  e.dumbbell_pair,e.load_mode,ws.set_index,ws.weight_kg,ws.reps,ws.duration_s,
                  COALESCE(ws.is_warmup,FALSE) AS is_warmup
           FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
           WHERE ws.workout_id=%s ORDER BY ws.set_index,ws.id""",
        (workout_id,),
    )
    sets = []
    volume = 0.0
    for row in rows:
        weight = float(row["weight_kg"]) if row.get("weight_kg") is not None else None
        timed = progression.is_timed_set(row)
        reps = progression.set_amount(row)
        is_warmup = bool(row["is_warmup"])
        if not is_warmup and not timed and weight and weight > 0 and reps > 0:
            multiplier = 1
            if row.get("equipment") == "dumbbell" and row.get("dumbbell_pair") is not False:
                multiplier = 2
            if row.get("load_mode") != "bodyweight":
                volume += weight * reps * multiplier
        sets.append({
            "id": int(row["id"]),
            "exercise_id": int(row["exercise_id"]),
            "exercise": row["exercise"],
            "muscle": row.get("muscle"),
            "set_index": int(row["set_index"]),
            "weight_kg": weight,
            "reps": reps,
            "is_warmup": is_warmup,
        })
    workout.update({
        "id": int(workout["id"]),
        "date": _date_text(workout.get("date")),
        "title": workout.get("title") or "Training",
        "duration_min": int(workout.get("duration_min") or 0),
        "sets": sets,
        "set_count": sum(not item["is_warmup"] for item in sets),
        "volume_kg": round(volume, 2),
    })
    return workout


def update_history_set(workout_id: int, set_id: int, weight_kg: float | None = None, reps: int | None = None) -> dict:
    if weight_kg is None and reps is None:
        raise TrackerError(400, "Keine Änderung angegeben")
    weight = _weight(weight_kg) if weight_kg is not None else None
    with db.cursor() as cur:
        cur.execute(
            """SELECT ws.id,ws.weight_kg,ws.reps,ws.duration_s,w.ended_at FROM workout_sets ws
               JOIN workouts w ON w.id=ws.workout_id WHERE ws.id=%s AND w.id=%s
               AND w.type IN ('gym','strength') FOR UPDATE OF ws,w""",
            (set_id, workout_id),
        )
        row = cur.fetchone()
        if not row:
            raise TrackerError(404, "Satz nicht gefunden")
        if row["ended_at"] is None:
            raise TrackerError(409, "Offene Sätze werden im Training bearbeitet")
        timed = progression.is_timed_set(row)
        count = _reps(reps, timed) if reps is not None else None
        new_weight = weight if weight is not None else row["weight_kg"]
        new_reps = count if count is not None else progression.set_amount(row)
        cur.execute(
            "UPDATE workout_sets SET weight_kg=%s,reps=%s,duration_s=%s WHERE id=%s",
            (new_weight, None if timed else new_reps, new_reps if timed else None, set_id),
        )
    return {"id": set_id, "workout_id": workout_id, "weight_kg": new_weight, "reps": new_reps}


def delete_history_set(workout_id: int, set_id: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            """SELECT ws.id,w.ended_at FROM workout_sets ws JOIN workouts w ON w.id=ws.workout_id
               WHERE ws.id=%s AND w.id=%s AND w.type IN ('gym','strength') FOR UPDATE OF ws,w""",
            (set_id, workout_id),
        )
        row = cur.fetchone()
        if not row:
            raise TrackerError(404, "Satz nicht gefunden")
        if row["ended_at"] is None:
            raise TrackerError(409, "Offene Sätze werden im Training gelöscht")
        cur.execute("DELETE FROM workout_sets WHERE id=%s", (set_id,))


def delete_workout(workout_id: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id,ended_at FROM workouts WHERE id=%s AND type IN ('gym','strength') FOR UPDATE",
            (workout_id,),
        )
        row = cur.fetchone()
        if not row:
            raise TrackerError(404, "Training nicht gefunden")
        if row["ended_at"] is None:
            raise TrackerError(409, "Ein offenes Training kann nicht aus dem Verlauf gelöscht werden")
        cur.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
