"""Übungsbibliothek und Workout-Verlauf mit synthetischen Daten."""

from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from core import db
from domains.gym import library
from domains.gym.aliases import normalize_alias
from domains.gym.tracker import TrackerError


@pytest.fixture
def library_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()

    suffix = uuid4()
    exercise_name = f"Synthetic Bench {suffix}"
    alias = f"Synthetic Press {suffix}"
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,muscle,unit,equipment,rep_min,rep_max)
           VALUES (%s,'strength','Brust','reps','barbell',6,10) RETURNING id""",
        (exercise_name,),
    )
    db.execute(
        """INSERT INTO exercise_aliases (alias,normalized_alias,exercise_id)
           VALUES (%s,%s,%s)""",
        (alias, normalize_alias(alias), exercise_id),
    )
    workout_date = date(2026, 10, 8)
    moment = datetime(2026, 10, 8, 17, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    workout_id = db.insert_returning(
        """INSERT INTO workouts (date,title,type,duration_min,created_at,last_activity_at,ended_at)
           VALUES (%s,%s,'gym',45,%s,%s,%s) RETURNING id""",
        (workout_date, "Synthetic Upper A", moment, moment, moment),
    )
    working_set_id = db.insert_returning(
        """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg,is_warmup)
           VALUES (%s,%s,1,10,100,FALSE) RETURNING id""",
        (workout_id, exercise_id),
    )
    warmup_set_id = db.insert_returning(
        """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg,is_warmup)
           VALUES (%s,%s,2,8,80,TRUE) RETURNING id""",
        (workout_id, exercise_id),
    )
    yield {
        "exercise_id": exercise_id,
        "exercise_name": exercise_name,
        "alias": alias,
        "workout_id": workout_id,
        "working_set_id": working_set_id,
        "warmup_set_id": warmup_set_id,
    }
    db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
    db.execute("DELETE FROM exercise_aliases WHERE normalized_alias=%s", (normalize_alias(alias),))
    db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


@pytest.mark.db
def test_library_search_e1rm_and_workout_history(library_database):
    data = library_database

    matches = library.list_exercises(data["alias"])
    assert [item["id"] for item in matches] == [data["exercise_id"]]

    detail = library.exercise_detail(data["exercise_id"])
    assert detail["history"][0]["e1rm_kg"] == 133.33
    assert detail["history"][0]["best_set"]["weight_kg"] == 100

    workout_summary = next(
        item for item in library.workout_list(limit=100)
        if item["id"] == data["workout_id"]
    )
    assert workout_summary["id"] == data["workout_id"]
    assert workout_summary["set_count"] == 1
    assert workout_summary["volume_kg"] == 1000
    workout = library.workout_detail(data["workout_id"])
    assert workout["set_count"] == 1
    assert workout["volume_kg"] == 1000

    corrected = library.update_history_set(
        data["workout_id"], data["working_set_id"], weight_kg=102.5, reps=9
    )
    assert corrected["weight_kg"] == 102.5
    assert corrected["reps"] == 9
    library.delete_history_set(data["workout_id"], data["warmup_set_id"])
    assert len(library.workout_detail(data["workout_id"])["sets"]) == 1

    library.delete_workout(data["workout_id"])
    with pytest.raises(TrackerError):
        library.workout_detail(data["workout_id"])
