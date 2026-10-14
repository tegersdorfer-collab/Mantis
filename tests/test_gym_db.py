"""PostgreSQL-Integration für Gym-Migrationen und atomare Sessionen."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from core import db
from domains.gym import session
from domains.gym.aliases import normalize_alias
from domains.gym.parser import ParsedLog, ParsedSet


pytestmark = pytest.mark.db


@pytest.fixture
def gym_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test in dieser Sandbox nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()
    if session.open_session():
        pytest.skip("mantis_test enthält bereits eine offene Gym-Session")
    yield


def _parsed(name: str, *, known=False):
    return ParsedLog(
        exercise=name,
        sets=(ParsedSet(40.0, 8), ParsedSet(40.0, 7)),
        known=known,
        alias=name,
    )


def test_repeated_migrations_preserve_legacy_workout_columns(gym_database):
    title = f"gym migration fixture {uuid4()}"
    workout_id = db.insert_returning(
        """INSERT INTO workouts (date,title,type,duration_min,notes)
           VALUES (CURRENT_DATE,%s,'strength',37,'synthetic fixture') RETURNING id""",
        (title,),
    )
    try:
        before = db.query_one(
            "SELECT date,title,type,duration_min,notes,created_at FROM workouts WHERE id=%s",
            (workout_id,),
        )
        db.run_migrations()
        db.run_migrations()
        after = db.query_one(
            "SELECT date,title,type,duration_min,notes,created_at,last_activity_at,ended_at "
            "FROM workouts WHERE id=%s",
            (workout_id,),
        )

        for field in ("date", "title", "type", "duration_min", "notes", "created_at"):
            assert after[field] == before[field]
        assert after["last_activity_at"] == before["created_at"]
        assert after["ended_at"] == before["created_at"]
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))


def test_unknown_exercise_error_rolls_back_the_new_workout(gym_database):
    name = f"Synthetic {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))

    with pytest.raises(ValueError, match="bestätigt"):
        session.add_sets(_parsed(name), allow_create=False, now=moment)

    assert db.query_one("SELECT id FROM workouts WHERE type='gym' AND ended_at IS NULL") is None
    assert db.query_one("SELECT id FROM exercises WHERE name=%s", (name,)) is None


def test_session_appends_corrects_and_finishes_in_one_workout(gym_database):
    name = f"Synthetic {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))
    parsed = _parsed(name, known=True)
    created = session.add_sets(parsed, allow_create=True, now=moment)
    workout_id = created["workout_id"]
    started = created["created_at"]
    try:
        followup = session.add_sets(
            ParsedLog(name, (ParsedSet(40.0, 6),), known=True, alias=name),
            allow_create=True,
            now=started + timedelta(minutes=2),
        )
        assert followup["workout_id"] == workout_id
        corrected = session.correct_last_reps(9, started + timedelta(minutes=10))
        assert corrected and corrected["reps"] == 9
        corrected_load = session.correct_last_load(
            name, 42.0, started + timedelta(minutes=12)
        )
        assert corrected_load and corrected_load["weight_kg"] == 42.0
        deleted = session.delete_last_set(started + timedelta(minutes=15))
        assert deleted and deleted["reps"] == 9
        sets = db.query(
            "SELECT reps,weight_kg,set_index FROM workout_sets WHERE workout_id=%s ORDER BY set_index",
            (workout_id,),
        )
        assert [row["reps"] for row in sets] == [8, 7]
        assert [row["weight_kg"] for row in sets] == [40.0, 40.0]

        summary = session.finish(started + timedelta(minutes=30))
        assert summary["workout_id"] == workout_id
        assert summary["duration_min"] == 15
        closed = db.query_one("SELECT ended_at FROM workouts WHERE id=%s", (workout_id,))
        assert closed["ended_at"] is not None
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        db.execute("DELETE FROM exercises WHERE name=%s", (name,))


def test_confirmed_unknown_exercise_creates_one_exercise_and_alias(gym_database):
    name = f"Synthetic confirmed {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))

    result = session.add_sets(_parsed(name), allow_create=True, now=moment)
    try:
        assert result["exercise"] == name
        assert db.query_one("SELECT COUNT(*) AS n FROM exercises WHERE name=%s", (name,))["n"] == 1
        alias = db.query_one(
            "SELECT exercise_id FROM exercise_aliases WHERE normalized_alias=%s",
            (normalize_alias(name),),
        )
        exercise = db.query_one("SELECT id FROM exercises WHERE name=%s", (name,))
        assert alias["exercise_id"] == exercise["id"]
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (result["workout_id"],))
        db.execute("DELETE FROM exercises WHERE name=%s", (name,))


def test_session_timeout_materializes_at_exact_two_hour_boundary(gym_database):
    name = f"Synthetic timeout {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))
    created = session.add_sets(_parsed(name, known=True), allow_create=True, now=moment)
    try:
        assert session.open_session(moment + timedelta(hours=2)) is None
        row = db.query_one(
            "SELECT ended_at,last_activity_at,duration_min FROM workouts WHERE id=%s",
            (created["workout_id"],),
        )
        assert row["ended_at"] == row["last_activity_at"] + timedelta(hours=2)
        assert row["duration_min"] == 0
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (created["workout_id"],))
        db.execute("DELETE FROM exercises WHERE name=%s", (name,))


def test_set_write_rolls_back_workout_and_exercise_after_database_error(gym_database, monkeypatch):
    name = f"Synthetic rollback {uuid4()}"

    def fail_after_insert(*_args):
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(session, "_exercise_rows", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        session.add_sets(
            _parsed(name, known=True), allow_create=True,
            now=datetime.now(ZoneInfo("Europe/Berlin")),
        )

    assert db.query_one("SELECT id FROM exercises WHERE name=%s", (name,)) is None
    assert db.query_one(
        "SELECT id FROM workouts WHERE type='gym' AND ended_at IS NULL"
    ) is None


def test_same_exercise_name_with_dumbbell_and_barbell_keeps_separate_history(gym_database):
    name = f"Synthetic KH-LH {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))
    db.execute(
        """INSERT INTO exercises
             (name, category, muscle, unit, exercise_type, equipment, rep_min, rep_max,
              rep_start, load_mode, weight_increment_kg, dumbbell_pair)
           VALUES (%s,'strength','chest','reps','compound','barbell',6,10,8,
                   'external',2.5,FALSE)""",
        (name,),
    )
    original = db.query_one(
        """SELECT id,name,equipment,exercise_type,rep_min,rep_max,rep_start,load_mode,
                  weight_increment_kg,dumbbell_pair FROM exercises WHERE name=%s""",
        (name,),
    )
    workout_id = None

    def log(equipment, minute):
        return session.add_sets(
            ParsedLog(
                exercise=name, sets=(ParsedSet(30.0, 10),), equipment=equipment,
                known=True, alias=name,
            ),
            allow_create=True,
            now=moment + timedelta(minutes=minute),
        )

    try:
        dumbbell = log("dumbbell", 0)
        workout_id = dumbbell["workout_id"]
        barbell = log("barbell", 2)
        log("dumbbell", 4)
        rows = db.query(
            """SELECT e.id,e.name,e.equipment,COUNT(*) AS sets
               FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s GROUP BY e.id,e.name,e.equipment ORDER BY e.id""",
            (dumbbell["workout_id"],),
        )
        after = db.query_one(
            """SELECT id,name,equipment,exercise_type,rep_min,rep_max,rep_start,load_mode,
                      weight_increment_kg,dumbbell_pair FROM exercises WHERE id=%s""",
            (original["id"],),
        )

        assert dumbbell["exercise"] == f"{name} (Kurzhantel)"
        assert barbell["exercise"] == name
        assert len(rows) == 2
        assert sorted(row["sets"] for row in rows) == [1, 2]
        assert original == after
    finally:
        if workout_id:
            db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        db.execute(
            "DELETE FROM exercises WHERE name=%s OR name=%s",
            (name, f"{name} (Kurzhantel)"),
        )


def test_rep_correction_with_wrong_old_value_does_not_update_database(gym_database):
    name = f"Synthetic correction {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))
    created = session.add_sets(_parsed(name, known=True), allow_create=True, now=moment)
    try:
        result = session.correct_last_reps(
            7, moment + timedelta(minutes=2), expected_reps=6
        )
        stored = db.query_one(
            "SELECT reps FROM workout_sets WHERE workout_id=%s ORDER BY id DESC LIMIT 1",
            (created["workout_id"],),
        )

        assert result == {"conflict": True, "reps": 7, "expected_reps": 6}
        assert stored["reps"] == 7
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (created["workout_id"],))
        db.execute("DELETE FROM exercises WHERE name=%s", (name,))


def test_warmup_marker_is_persisted_and_excluded_from_progression(gym_database):
    name = f"Synthetic warmup {uuid4()}"
    moment = datetime.now(ZoneInfo("Europe/Berlin"))
    result = session.add_sets(
        ParsedLog(
            exercise=name,
            sets=(ParsedSet(40.0, 10, True), ParsedSet(80.0, 8)),
            known=True,
            alias=name,
        ),
        allow_create=True,
        now=moment,
    )
    try:
        rows = db.query(
            "SELECT is_warmup,reps FROM workout_sets WHERE workout_id=%s ORDER BY set_index",
            (result["workout_id"],),
        )

        assert [(row["is_warmup"], row["reps"]) for row in rows] == [(True, 10), (False, 8)]
        assert [
            (row["reps"], row["weight_kg"], row["is_warmup"])
            for row in result["current_sets"]
        ] == [(8, 80.0, False)]
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (result["workout_id"],))
        db.execute("DELETE FROM exercises WHERE name=%s", (name,))

