"""Synthetische DB-Integration für Gym-Statistiken und Körpermessungen."""

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest

from core import db
from domains.gym import library, overview


pytestmark = pytest.mark.db


def _test_date() -> date:
    year = 2300 + uuid4().int % 1000
    return date(year, 6, 17)


def test_dashboard_counts_synthetic_workouts_and_consecutive_weeks():
    try:
        db.query_one("SELECT 1 AS ready")
        db.run_migrations()
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")

    today = _test_date()
    monday = today - timedelta(days=today.weekday())
    name = f"Synthetic Overview {uuid4()}"
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,muscle,unit,equipment,rep_min,rep_max)
           VALUES (%s,'strength','Brust','reps','barbell',6,10) RETURNING id""",
        (name,),
    )
    workout_ids = []
    try:
        before = overview.dashboard(weeks=4, today=today)
        for week_offset in (2, 1, 0):
            workout_date = monday - timedelta(weeks=week_offset)
            moment = datetime.combine(workout_date, time(12), tzinfo=timezone.utc)
            workout_id = db.insert_returning(
                """INSERT INTO workouts
                     (date,title,type,duration_min,created_at,last_activity_at,ended_at)
                   VALUES (%s,%s,'gym',30,%s,%s,%s) RETURNING id""",
                (workout_date, f"Synthetic overview {uuid4()}", moment, moment, moment),
            )
            workout_ids.append(workout_id)
            db.execute(
                """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg)
                   VALUES (%s,%s,1,10,100)""",
                (workout_id, exercise_id),
            )

        result = overview.dashboard(weeks=4, today=today)

        assert result["workout_count"] - before["workout_count"] == 3
        assert result["streak_weeks"] == 3
        weeks = {row["week_start"]: row for row in result["weekly"]}
        assert weeks[monday.isoformat()]["workout_count"] == 1
        assert weeks[monday.isoformat()]["volume_kg"] == 1000
        assert weeks[(monday - timedelta(weeks=1)).isoformat()]["workout_count"] == 1
        assert weeks[(monday - timedelta(weeks=2)).isoformat()]["workout_count"] == 1
    finally:
        for workout_id in workout_ids:
            db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


def test_body_measurement_upsert_reuses_existing_body_measurements_table():
    try:
        db.query_one("SELECT 1 AS ready")
        db.run_migrations()
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")

    measured_on = _test_date()
    try:
        first = overview.save_measurement(72.4, 18.5, date_=measured_on)
        second = overview.save_measurement(71.9, None, date_=measured_on)
        stored = db.query_one(
            "SELECT id,weight_kg,body_fat FROM body_measurements WHERE date=%s",
            (measured_on,),
        )
        assert first["id"] == second["id"] == stored["id"]
        assert stored["weight_kg"] == 71.9
        assert stored["body_fat"] == 18.5
        result = overview.dashboard(weeks=4, today=measured_on)
        assert result["latest_measurement"]["date"] == measured_on.isoformat()
        assert result["latest_measurement"]["weight_kg"] == 71.9
    finally:
        db.execute("DELETE FROM body_measurements WHERE date=%s", (measured_on,))


def test_exercise_instructions_are_persisted_on_the_exercise():
    try:
        db.query_one("SELECT 1 AS ready")
        db.run_migrations()
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")

    name = f"Synthetic Instructions {uuid4()}"
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,muscle,unit)
           VALUES (%s,'strength','Rücken','reps') RETURNING id""",
        (name,),
    )
    try:
        updated = library.update_instructions(exercise_id, "Zug bis zur Brust führen.")
        assert updated["instructions"] == "Zug bis zur Brust führen."
        assert library.exercise_detail(exercise_id)["instructions"] == "Zug bis zur Brust führen."
    finally:
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))
