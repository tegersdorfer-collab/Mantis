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


@pytest.fixture
def hang(tracker_database):
    name = f"Synthetic Deadhang {uuid4()}"
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,unit,load_mode,rep_min,rep_max)
           VALUES (%s,'strength','min','added',30,60) RETURNING id""",
        (name,),
    )
    tracker_database.setdefault("new_exercises", []).append(exercise_id)
    tracker_database["hang_id"] = exercise_id
    tracker_database["hang_name"] = name
    return tracker_database


def _stored(client_id):
    return db.query_one(
        "SELECT reps,duration_s,weight_kg,exercise_id FROM workout_sets WHERE client_id=%s", (client_id,)
    )


def test_live_logged_timed_set_lands_in_duration_s_and_seed_still_sees_seconds(hang):
    from domains.gym import plans

    client_id = _client_id(hang)
    saved = tracker.log_set(client_id, hang["hang_name"], 20, 120, now=NOW)

    row = _stored(client_id)
    assert row["reps"] is None and row["duration_s"] == 120
    assert saved["reps"] == 120
    source = db.query_one("SELECT id,unit FROM exercises WHERE id=%s", (hang["hang_id"],))
    sets = db.query("SELECT duration_s,distance_km FROM workout_sets WHERE client_id=%s", (client_id,))
    assert plans._is_timed(source, sets)
    assert tracker.history(hang["hang_name"], limit=1)[0]["sets"] == [{"weight_kg": 20.0, "reps": 120}]
    assert tracker.update_set(saved["id"], None, 150, now=NOW)["reps"] == 150
    row = _stored(client_id)
    assert row["reps"] is None and row["duration_s"] == 150


def test_timed_sets_allow_up_to_600_seconds_and_reps_stay_capped(hang):
    tracker.log_set(_client_id(hang), hang["hang_name"], 20, 600, now=NOW)
    for exercise, value in ((hang["hang_name"], 601), (hang["hang_name"], 0), ("Squats", 101)):
        with pytest.raises(tracker.TrackerError) as error:
            tracker.log_set(str(uuid4()), exercise, 20, value, now=NOW)
        assert error.value.status == 400
    client_id = _client_id(hang)
    tracker.log_set(client_id, "Squats", 100, 100, now=NOW)
    row = _stored(client_id)
    assert (row["reps"], row["duration_s"]) == (100, None)


def test_timed_history_and_library_read_duration_s(hang):
    from domains.gym import library

    tracker.log_set(_client_id(hang), hang["hang_name"], 20, 45, now=NOW)
    tracker.finish_session(now=NOW + timedelta(minutes=5))

    detail = library.exercise_detail(hang["hang_id"])

    assert detail["history"][0]["sets"][0]["reps"] == 45
    assert detail["history"][0]["sets"][0]["unit"] == "seconds"
    assert detail["history"][0]["e1rm_kg"] is None
    workout_id = detail["history"][0]["workout_id"]
    set_id = detail["history"][0]["sets"][0]["id"]
    workout_set = library.workout_detail(workout_id)["sets"][0]
    assert workout_set["reps"] == 45
    assert workout_set["unit"] == "seconds"
    library.update_history_set(workout_id, set_id, None, 300)
    row = db.query_one("SELECT reps,duration_s FROM workout_sets WHERE id=%s", (set_id,))
    assert (row["reps"], row["duration_s"]) == (None, 300)


def test_tracker_prefers_session_target_unit_over_exercise_unit(monkeypatch):
    name = "Synthetic plan-timed reps exercise"
    stored = {}
    observed = {}
    exercise_units = {"unit": "reps", "target_unit": "seconds"}

    def query_one(sql, _params=()):
        if "FROM exercises e" in sql:
            return exercise_units.copy()
        if "WHERE ws.client_id=%s" in sql:
            return stored.copy() if stored else None
        raise AssertionError(f"unexpected query: {sql}")

    def add_sets(parsed, **_kwargs):
        saved_set = parsed.sets[0]
        observed["parsed_set"] = saved_set
        stored.update({
            "id": 7, "exercise": name, "weight_kg": saved_set.weight_kg,
            "reps": saved_set.reps, "duration_s": saved_set.duration_s,
            "set_index": 1, "workout_id": 77,
        })

    class Cursor:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

    cursor = Cursor()
    monkeypatch.setattr(tracker, "_exercise_name", lambda value: value)
    monkeypatch.setattr(tracker.session, "open_session", lambda now=None: {"id": 77})
    monkeypatch.setattr(tracker.session, "add_sets", add_sets)
    monkeypatch.setattr(tracker.db, "query_one", query_one)
    monkeypatch.setattr(tracker.db, "cursor", lambda: cursor)
    monkeypatch.setattr(tracker.session, "_lock_session", lambda _cur: None)

    saved = tracker.log_set("synthetic-client", name, 20, 120, now=NOW)

    assert observed["parsed_set"].reps is None
    assert observed["parsed_set"].duration_s == 120
    assert saved["reps"] == 120

    exercise_units.update(unit="min", target_unit="reps")
    with pytest.raises(tracker.TrackerError) as error:
        tracker.log_set("second-client", name, 20, 120, now=NOW)
    assert error.value.status == 400

    update_row = {
        "id": 7, "workout_id": 77, "exercise_id": 1, "set_index": 1,
        "weight_kg": 20.0, "reps": 8, "duration_s": None,
        "unit": "reps", "target_unit": "seconds", "ended_at": None,
        "exercise": name,
    }
    monkeypatch.setattr(
        tracker, "_check_open_set", lambda *_args: (update_row, None),
    )

    updated = tracker.update_set(7, None, 150, now=NOW)

    assert updated["reps"] == 150
    assert cursor.statements[-2][1] == (20.0, None, 150, 7)


def test_adhoc_added_timed_exercise_logs_seconds_in_duration_s(hang):
    from domains.gym import plans

    first = _client_id(hang)
    tracker.log_set(first, "Squats", 100, 5, now=NOW)
    workout_id = db.query_one(
        "SELECT w.id FROM workouts w JOIN workout_sets s ON s.workout_id=w.id WHERE s.client_id=%s",
        (first,),
    )["id"]
    entry = plans.add_session_exercise(workout_id, hang["hang_id"])
    assert entry["target_unit"] == "seconds"

    client_id = _client_id(hang)
    saved = tracker.log_set(client_id, hang["hang_name"], 20, 120, now=NOW)
    row = _stored(client_id)
    assert (row["reps"], row["duration_s"]) == (None, 120)
    tracker.update_set(saved["id"], None, 150, now=NOW)
    row = _stored(client_id)
    assert (row["reps"], row["duration_s"]) == (None, 150)
