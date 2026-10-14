"""Session-Zeitgrenzen und Abschlusszusammenfassung."""

from datetime import datetime, timedelta, timezone

from domains.gym.session import _exercise_defaults, session_timeout_end, summarize_session
from domains.gym import session
from domains.gym.parser import ParsedLog, ParsedSet


def test_timeout_uses_last_activity_plus_exactly_two_hours():
    last = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)

    assert session_timeout_end(last) == last + timedelta(hours=2)


def test_lateral_raise_defaults_to_the_isolation_rep_range():
    defaults = _exercise_defaults("Lateral Raise", None, "external")

    assert defaults["exercise_type"] == "isolation"
    assert (defaults["rep_min"], defaults["rep_max"], defaults["rep_start"]) == (8, 12, 10)


def test_summary_duration_stops_at_last_workout_action():
    start = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    last = start + timedelta(minutes=47, seconds=50)
    summary = summarize_session([], start, last)

    assert summary["duration_min"] == 47


def test_summary_counts_sets_and_double_dumbbell_volume_only_for_pairs():
    start = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    rows = [
        {"exercise": "Schulterdrücken", "equipment": "dumbbell", "weight_kg": 22.5, "reps": 10},
        {"exercise": "Klimmzüge", "load_mode": "added", "weight_kg": 10, "reps": 8},
        {"exercise": "Klimmzüge", "load_mode": "bodyweight", "weight_kg": None, "reps": 6},
    ]

    summary = summarize_session(rows, start, start + timedelta(minutes=30))

    assert summary == {
        "duration_min": 30,
        "exercise_count": 2,
        "set_count": 3,
        "volume_kg": 530.0,
        "exercises": ["Klimmzüge", "Schulterdrücken"],
    }


def test_summary_excludes_warmup_volume_but_counts_working_sets():
    start = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    rows = [
        {"exercise": "Bankdrücken", "weight_kg": 40, "reps": 10, "is_warmup": True},
        {"exercise": "Bankdrücken", "weight_kg": 80, "reps": 8, "is_warmup": False},
    ]

    summary = summarize_session(rows, start, start)

    assert summary["set_count"] == 1
    assert summary["volume_kg"] == 640.0


def test_equipment_variants_do_not_reuse_or_mutate_a_different_equipment_exercise(monkeypatch):
    rows = {
        1: {
            "id": 1, "name": "Synthetic Press", "exercise_type": "compound",
            "equipment": "barbell", "rep_min": 6, "rep_max": 10, "rep_start": 8,
            "load_mode": "external", "weight_increment_kg": 2.5, "dumbbell_pair": False,
        },
    }

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

    cur = Cursor()

    def fetchall(_cur, _sql, params=()):
        names = {value.casefold() for value in params}
        if not names:
            return [row.copy() for row in rows.values()]
        return [row.copy() for row in rows.values() if row["name"].casefold() in names]

    def fetchone(_cur, sql, params=()):
        if sql.lstrip().startswith("SELECT id, name"):
            return next(
                (row.copy() for row in rows.values() if row["name"].casefold() == params[0].casefold()),
                None,
            )
        if sql.lstrip().startswith("INSERT INTO exercises"):
            values = params
            row = {
                "id": max(rows) + 1, "name": values[0],
                "exercise_type": values[4], "equipment": values[5],
                "rep_min": values[6], "rep_max": values[7], "rep_start": values[8],
                "load_mode": values[9], "weight_increment_kg": values[10],
                "dumbbell_pair": values[11],
            }
            rows[row["id"]] = row
            return row.copy()
        if sql.lstrip().startswith("UPDATE exercises SET"):
            return rows[params[-1]].copy()
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(session, "_fetchall", fetchall)
    monkeypatch.setattr(session, "_fetchone", fetchone)
    original = rows[1].copy()

    dumbbell = session._ensure_exercise(
        cur, "Synthetic Press", "Synthetic Press", "dumbbell", "external", allow_create=True
    )
    repeated = session._ensure_exercise(
        cur, "Synthetic Press", "Synthetic Press", "dumbbell", "external", allow_create=True
    )
    barbell = session._ensure_exercise(
        cur, "Synthetic Press", "Synthetic Press", "barbell", "external", allow_create=True
    )

    assert dumbbell["name"] == "Synthetic Press (Kurzhantel)"
    assert repeated["id"] == dumbbell["id"]
    assert barbell["id"] == 1
    assert rows[1] == original


def test_equipment_prefix_resolves_to_a_legacy_exercise_name_without_duplication(monkeypatch):
    legacy = [
        {"id": 10, "name": "KH Schulterdrücken", "equipment": None, "load_mode": "external"},
        {"id": 11, "name": "LH Schulterdrücken", "equipment": None, "load_mode": "external"},
    ]
    monkeypatch.setattr(session, "_fetchall", lambda *_args, **_kwargs: legacy)

    resolved = session._equipment_variant_name(
        None, "Schulterdrücken", "dumbbell", "external"
    )

    assert resolved == "KH Schulterdrücken"


def test_reps_correction_conflict_does_not_issue_an_update(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

    cur = Cursor()
    moment = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(session.db, "cursor", lambda: cur)
    monkeypatch.setattr(session, "_open_row", lambda _cur: {
        "id": 1, "created_at": moment, "last_activity_at": moment,
    })
    monkeypatch.setattr(session, "_fetchone", lambda *_args, **_kwargs: {
        "id": 2, "exercise_id": 3, "set_index": 1, "reps": 9,
    })
    monkeypatch.setattr(session, "_exercise_state", lambda *_args: {})

    result = session.correct_last_reps(7, moment, expected_reps=8)

    assert result == {"conflict": True, "reps": 9, "expected_reps": 8}
    assert not any("UPDATE workout_sets" in sql for sql, _params in cur.statements)


def test_add_sets_writes_the_warmup_marker_to_each_set(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

    cur = Cursor()
    moment = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    workout = {
        "id": 1, "date": moment.date(), "created_at": moment,
        "last_activity_at": moment, "ended_at": None,
    }
    exercise = {
        "id": 2, "name": "Synthetic Press", "exercise_type": "compound",
        "equipment": "barbell", "rep_min": 6, "rep_max": 10, "rep_start": 8,
        "load_mode": "external", "weight_increment_kg": 2.5, "dumbbell_pair": False,
    }
    monkeypatch.setattr(session.db, "cursor", lambda: cur)
    monkeypatch.setattr(session, "_open_row", lambda _cur: workout)
    monkeypatch.setattr(session, "_ensure_exercise", lambda *_args, **_kwargs: exercise)
    monkeypatch.setattr(session, "_fetchone", lambda *_args, **_kwargs: {"last_index": 0})
    monkeypatch.setattr(session, "_exercise_rows", lambda *_args: [])
    monkeypatch.setattr(session, "_planned_set_count", lambda *_args: None)

    result = session.add_sets(
        ParsedLog(
            exercise="Synthetic Press",
            sets=(ParsedSet(40, 10, True), ParsedSet(80, 8)),
            known=True,
        ),
        allow_create=True,
        now=moment,
    )

    inserts = [
        params for sql, params in cur.statements if "INSERT INTO workout_sets" in sql
    ]
    assert [params[-1] for params in inserts] == [True, False]
    assert [item["is_warmup"] for item in result["sets"]] == [True, False]

