"""Synthetische PostgreSQL-Integrationstests für den Web-Tracker."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg2
import pytest

from core import db
from domains.gym import session, tracker


pytestmark = pytest.mark.db
NOW = datetime(2035, 5, 20, 9, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def tracker_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()
    if session.open_session():
        pytest.skip("mantis_test enthält bereits eine offene Gym-Session")

    created = {"exercises": {}, "workouts": [], "client_ids": []}
    for name in (*tracker.EXERCISES, "Bankdrücken"):
        row = db.query_one("SELECT id FROM exercises WHERE name=%s", (name,))
        if row:
            created["exercises"][name] = row["id"]
        else:
            exercise_id = db.insert_returning(
                "INSERT INTO exercises (name,category,unit) "
                "VALUES (%s,'strength','reps') RETURNING id",
                (name,),
            )
            created["exercises"][name] = exercise_id
            created.setdefault("new_exercises", []).append(exercise_id)

    yield created

    for client_id in created["client_ids"]:
        row = db.query_one(
            "SELECT workout_id FROM workout_sets WHERE client_id=%s", (client_id,)
        )
        if row and row["workout_id"] not in created["workouts"]:
            created["workouts"].append(row["workout_id"])
    for workout_id in created["workouts"]:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
    for exercise_id in created.get("new_exercises", []):
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


@pytest.fixture
def old_bench(tracker_database):
    ids = tracker_database["exercises"]
    old = NOW - timedelta(days=1)
    workout_id = db.insert_returning(
        "INSERT INTO workouts (date,title,type,last_activity_at,ended_at) "
        "VALUES (%s,'synthetic old bench','gym',%s,%s) RETURNING id",
        (old.date(), old, old),
    )
    tracker_database["workouts"].append(workout_id)
    rows = [
        (ids["Bench Press"], 1, 40.0, 10, True),
        (ids["Bench Press"], 2, 60.0, 8, False),
        (ids["Bankdrücken"], 3, 60.0, 7, False),
    ]
    for exercise_id, index, weight, reps, warmup in rows:
        db.execute(
            "INSERT INTO workout_sets "
            "(workout_id,exercise_id,set_index,weight_kg,reps,is_warmup) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (workout_id, exercise_id, index, weight, reps, warmup),
        )
    return workout_id


def _client_id(database):
    value = str(uuid4())
    database["client_ids"].append(value)
    return value


def test_client_id_migration_enforces_unique_non_null_ids(tracker_database):
    index = db.query_one(
        "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
        "AND indexname='workout_sets_client_id_idx'"
    )
    assert index and "UNIQUE" in index["indexdef"].upper()
    assert "WHERE (client_id IS NOT NULL)" in index["indexdef"]

    workout_id = db.insert_returning(
        "INSERT INTO workouts (date,title,type,ended_at) "
        "VALUES (%s,'synthetic idempotency fixture','strength',%s) RETURNING id",
        (NOW.date(), NOW),
    )
    tracker_database["workouts"].append(workout_id)
    exercise_id = tracker_database["exercises"]["Squats"]
    params = (workout_id, exercise_id, 1, 5, 20.0, "synthetic-client-id")
    db.execute(
        "INSERT INTO workout_sets "
        "(workout_id,exercise_id,set_index,reps,weight_kg,client_id) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        params,
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.execute(
            "INSERT INTO workout_sets "
            "(workout_id,exercise_id,set_index,reps,weight_kg,client_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (workout_id, exercise_id, 2, 5, 20.0, "synthetic-client-id"),
        )


def test_prefill_uses_last_session_working_sets_and_merges_legacy_names(old_bench):
    state = tracker.get_state(now=NOW)
    bench = next(item for item in state["exercises"] if item["name"] == "Bench Press")

    assert bench["prefill"] == [
        {"weight_kg": 60.0, "reps": 8},
        {"weight_kg": 60.0, "reps": 7},
    ]
    assert [item["name"] for item in state["exercises"]] == [
        "Squats", "Bench Press", "Deadlifts",
    ]


def test_prefill_is_empty_without_history(tracker_database):
    if session.exercise_history("Squats", 1):
        pytest.skip("mantis_test enthält bereits synthetische Squats-Historie")

    squats = next(
        item for item in tracker.get_state(now=NOW)["exercises"]
        if item["name"] == "Squats"
    )
    assert squats["prefill"] == [] and squats["logged"] == []


def test_log_set_opens_session_and_is_idempotent(tracker_database):
    client_id = _client_id(tracker_database)
    first = tracker.log_set(client_id, "Bench Press", 82.5, 8, now=NOW)
    duplicate = tracker.log_set(client_id, "Bench Press", 82.5, 8, now=NOW)

    assert first["id"] == duplicate["id"]
    assert duplicate["duplicate"] is True
    assert db.query_one(
        "SELECT count(*) AS n FROM workout_sets WHERE client_id=%s", (client_id,)
    )["n"] == 1
    assert tracker.get_state(now=NOW)["session"]["id"] == first["workout_id"]


def test_log_set_rejects_unknown_exercises_and_out_of_range_values(tracker_database):
    invalid = [
        ("Curl", 20, 10),
        ("Squats", 0, 5),
        ("Squats", 500.25, 5),
        ("Squats", 100, 0),
        ("Squats", 100, 101),
    ]
    for exercise, weight, reps in invalid:
        with pytest.raises(tracker.TrackerError) as error:
            tracker.log_set(str(uuid4()), exercise, weight, reps, now=NOW)
        assert error.value.status == 400


def test_positive_weight_that_rounds_to_zero_is_rejected():
    with pytest.raises(tracker.TrackerError) as error:
        tracker._weight(0.01)

    assert error.value.status == 400


def test_update_and_delete_are_limited_to_the_open_session(tracker_database):
    client_id = _client_id(tracker_database)
    saved = tracker.log_set(client_id, "Squats", 100, 5, now=NOW)
    updated = tracker.update_set(saved["id"], 102.62, None, now=NOW)
    assert updated["weight_kg"] == 102.5

    tracker.finish_session(now=NOW + timedelta(minutes=30))
    for operation in (
        lambda: tracker.update_set(saved["id"], None, 6, now=NOW),
        lambda: tracker.delete_set(saved["id"], now=NOW),
    ):
        with pytest.raises(tracker.TrackerError) as error:
            operation()
        assert error.value.status == 409

    for operation in (
        lambda: tracker.update_set(999999999, 100, None, now=NOW),
        lambda: tracker.delete_set(999999999, now=NOW),
    ):
        with pytest.raises(tracker.TrackerError) as error:
            operation()
        assert error.value.status == 404


def test_timeout_closes_the_old_session_before_logging_again(tracker_database):
    first_id = _client_id(tracker_database)
    second_id = _client_id(tracker_database)
    first = tracker.log_set(first_id, "Squats", 100, 5, now=NOW)
    second = tracker.log_set(
        second_id, "Squats", 100, 5,
        now=NOW + timedelta(hours=2, seconds=1),
    )

    assert first["workout_id"] != second["workout_id"]


def test_finish_summary_and_history(tracker_database):
    tracker.log_set(_client_id(tracker_database), "Deadlifts", 140, 5, now=NOW)
    tracker.log_set(_client_id(tracker_database), "Deadlifts", 140, 4, now=NOW)

    summary = tracker.finish_session(now=NOW + timedelta(minutes=30))
    history = tracker.history("Deadlifts", limit=5)

    assert summary["sets"] == 2 and summary["volume_kg"] == 1260.0
    assert history[0]["sets"] == [
        {"weight_kg": 140.0, "reps": 5},
        {"weight_kg": 140.0, "reps": 4},
    ]
