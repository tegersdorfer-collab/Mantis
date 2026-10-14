"""Schlanke Domain-Fassade für den Web-Tracker der drei Grundübungen."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import psycopg2

from core import db
from domains.gym import session
from domains.gym.parser import ParsedLog, ParsedSet


EXERCISES: tuple[str, ...] = ("Squats", "Bench Press", "Deadlifts")
REST_SECONDS: dict[str, int] = {
    "Squats": 180,
    "Bench Press": 180,
    "Deadlifts": 180,
}


class TrackerError(Exception):
    """Ein fachlicher Tracker-Fehler mit passendem HTTP-Status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _exercise_name(value: str) -> str:
    if value not in EXERCISES:
        raise TrackerError(400, "Unbekannte Übung")
    return value


def _weight(value: float) -> float:
    try:
        weight = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise TrackerError(400, "Gewicht muss größer als 0 und höchstens 500 kg sein") from None
    if not weight.is_finite() or weight <= 0 or weight > 500:
        raise TrackerError(400, "Gewicht muss größer als 0 und höchstens 500 kg sein")
    rounded = (weight * 4).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 4
    if rounded <= 0:
        raise TrackerError(400, "Gewicht muss nach dem Runden größer als 0 sein")
    return float(rounded)


def _reps(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise TrackerError(400, "Wiederholungen müssen zwischen 1 und 100 liegen")
    return value


def _set_row(client_id: str) -> dict | None:
    return db.query_one(
        """SELECT ws.id, e.name AS exercise, ws.weight_kg, ws.reps,
                  ws.set_index, ws.workout_id
           FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
           WHERE ws.client_id=%s""",
        (client_id,),
    )


def _set_result(row: dict, *, duplicate: bool) -> dict:
    return {
        "id": int(row["id"]),
        "exercise": row["exercise"],
        "weight_kg": float(row["weight_kg"]) if row.get("weight_kg") is not None else None,
        "reps": int(row["reps"]),
        "set_index": int(row["set_index"]),
        "workout_id": int(row["workout_id"]),
        "duplicate": duplicate,
    }


def log_set(
    client_id: str, exercise: str, weight_kg: float, reps: int,
    now: datetime | None = None,
) -> dict:
    name = _exercise_name(exercise)
    weight = _weight(weight_kg)
    count = _reps(reps)
    if not isinstance(client_id, str) or not client_id.strip():
        raise TrackerError(400, "client_id fehlt")

    existing = _set_row(client_id)
    if existing:
        return _set_result(existing, duplicate=True)

    try:
        session.add_sets(
            ParsedLog(
                exercise=name,
                sets=(ParsedSet(weight, count),),
                known=True,
                alias=name,
            ),
            now=now,
            client_id=client_id,
        )
    except psycopg2.errors.UniqueViolation:
        # Ein konkurrierender Retry kann zwischen Vorabfrage und INSERT gewinnen.
        existing = _set_row(client_id)
        if existing:
            return _set_result(existing, duplicate=True)
        raise

    created = _set_row(client_id)
    if not created:
        raise TrackerError(500, "Der gespeicherte Satz konnte nicht geladen werden")
    return _set_result(created, duplicate=False)


def _exercise_ids(name: str) -> list[int]:
    rows = db.query("SELECT id, name, equipment FROM exercises")
    return session._legacy_duplicate_ids(rows, name) or [
        row["id"] for row in rows if row["name"].casefold() == name.casefold()
    ]


def _set_values(rows: list[dict]) -> list[dict]:
    return [
        {
            "weight_kg": float(row["weight_kg"]) if row.get("weight_kg") is not None else None,
            "reps": int(row["reps"]),
        }
        for row in rows
    ]


def _iso(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def get_state(now: datetime | None = None) -> dict:
    opened = session.open_session(now=now)
    open_id = opened["id"] if opened else None
    exercises = []

    for name in EXERCISES:
        history = session.exercise_history(name, limit=2)
        prior = next((item for item in history if item["workout_id"] != open_id), None)
        logged = []
        if open_id is not None:
            ids = _exercise_ids(name)
            if ids:
                logged = db.query(
                    """SELECT ws.id, ws.weight_kg, ws.reps, ws.set_index
                       FROM workout_sets ws
                       WHERE ws.workout_id=%s AND ws.exercise_id=ANY(%s)
                         AND COALESCE(ws.is_warmup,FALSE)=FALSE
                       ORDER BY ws.set_index, ws.id""",
                    (open_id, ids),
                )
        exercises.append({
            "name": name,
            "rest_s": REST_SECONDS[name],
            "prefill": _set_values(prior["sets"]) if prior else [],
            "logged": [
                {
                    "id": int(row["id"]),
                    "weight_kg": float(row["weight_kg"]) if row["weight_kg"] is not None else None,
                    "reps": int(row["reps"]),
                    "set_index": int(row["set_index"]),
                }
                for row in logged
            ],
        })

    session_state = None
    if opened:
        session_state = {
            "id": int(opened["id"]),
            "date": _iso(opened["date"]),
            "started_at": _iso(opened["created_at"]),
        }
    return {"session": session_state, "exercises": exercises}


def _open_set_row(cur, set_id: int) -> dict | None:
    cur.execute(
        """SELECT ws.id, ws.workout_id, ws.exercise_id, ws.set_index,
                  ws.weight_kg, ws.reps, w.ended_at, e.name AS exercise
           FROM workout_sets ws JOIN workouts w ON w.id=ws.workout_id
           JOIN exercises e ON e.id=ws.exercise_id
           WHERE ws.id=%s FOR UPDATE OF ws, w""",
        (set_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _check_open_set(cur, set_id: int, moment: datetime):
    row = _open_set_row(cur, set_id)
    if row is None:
        return None, (404, "Satz nicht gefunden")

    opened = session._open_row(cur)
    if opened and moment >= session.session_timeout_end(
        opened.get("last_activity_at") or opened["created_at"]
    ):
        session._close_timed_out(cur, opened)
        opened = None
    if (
        row["ended_at"] is not None
        or opened is None
        or row["workout_id"] != opened["id"]
    ):
        return row, (409, "Satz liegt nicht in der offenen Session")
    return row, None


def update_set(
    set_id: int, weight_kg: float | None, reps: int | None,
    now: datetime | None = None,
) -> dict:
    if weight_kg is None and reps is None:
        raise TrackerError(400, "Keine Änderung angegeben")
    weight = _weight(weight_kg) if weight_kg is not None else None
    count = _reps(reps) if reps is not None else None
    moment = session.local_now(now)
    session.open_session(now=moment)
    error = None
    result = None
    with db.cursor() as cur:
        session._lock_session(cur)
        row, error = _check_open_set(cur, set_id, moment)
        if row is not None and error is None:
            weight = weight if weight is not None else row["weight_kg"]
            count = count if count is not None else row["reps"]
            cur.execute(
                "UPDATE workout_sets SET weight_kg=%s,reps=%s WHERE id=%s",
                (weight, count, set_id),
            )
            cur.execute(
                "UPDATE workouts SET last_activity_at=%s WHERE id=%s AND ended_at IS NULL",
                (moment, row["workout_id"]),
            )
            result = {
                "id": int(row["id"]),
                "exercise": row["exercise"],
                "weight_kg": float(weight) if weight is not None else None,
                "reps": int(count),
                "set_index": int(row["set_index"]),
                "workout_id": int(row["workout_id"]),
            }
    if error:
        raise TrackerError(*error)
    return result


def delete_set(set_id: int, now: datetime | None = None) -> None:
    moment = session.local_now(now)
    session.open_session(now=moment)
    error = None
    with db.cursor() as cur:
        session._lock_session(cur)
        row, error = _check_open_set(cur, set_id, moment)
        if row is not None and error is None:
            cur.execute("DELETE FROM workout_sets WHERE id=%s", (set_id,))
            cur.execute(
                "UPDATE workouts SET last_activity_at=%s WHERE id=%s AND ended_at IS NULL",
                (moment, row["workout_id"]),
            )
    if error:
        raise TrackerError(*error)


def finish_session(now: datetime | None = None) -> dict | None:
    result = session.finish(now=now)
    if result is None:
        return None
    return {
        "workout_id": int(result["workout_id"]),
        "duration_min": int(result["duration_min"]),
        "sets": int(result["set_count"]),
        "volume_kg": float(result["volume_kg"]),
    }


def history(exercise: str, limit: int = 5) -> list[dict]:
    name = _exercise_name(exercise)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise TrackerError(400, "limit muss zwischen 1 und 20 liegen")
    rows = session.exercise_history(name, limit=limit)
    return [
        {
            "date": _iso(item["date"]),
            "sets": [
                {
                    "weight_kg": (
                        float(row["weight_kg"]) if row.get("weight_kg") is not None else None
                    ),
                    "reps": int(row["reps"]),
                }
                for row in item["sets"]
            ],
        }
        for item in rows
    ]
