"""Synthetische AlphaProgression-Textimporte."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from core import db
from domains.gym import ap_text_import
from domains.gym.ap_text_import import (
    APSet,
    APExercise,
    APWorkout,
    ImportReport,
    WorkoutReportEntry,
    _canonical_exercise,
    _equipment_code,
    _variant_name,
    import_workouts,
    parse_export,
)
from scripts import import_ap_text


FIXTURE = Path(__file__).parent / "fixtures" / "ap_export_synthetic.txt"


@pytest.fixture
def synthetic_exercise_rows():
    return [
        {"id": 18, "name": "Curls", "equipment": None, "unit": "reps",
         "load_mode": "external"},
        {"id": 23, "name": "Curls (KH)", "equipment": "dumbbell", "unit": "reps",
         "load_mode": "external"},
    ]


def _exercise(name, equipment):
    return APExercise(name, equipment, "reps", ())


@pytest.fixture
def synthetic_import_database(monkeypatch):
    def install(exercise_rows, workout_rows=()):
        class Cursor:
            def execute(self, sql, _params=()):
                self.sql = sql
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                    raise AssertionError("Dry-run darf keine Datenbankzeilen schreiben")

            def fetchall(self):
                if "FROM exercises" in self.sql:
                    return [dict(row) for row in exercise_rows]
                if "FROM workouts" in self.sql:
                    return [dict(row) for row in workout_rows]
                return []

            def fetchone(self):
                return None

        cursor = Cursor()

        @contextmanager
        def open_cursor():
            yield cursor

        monkeypatch.setattr(db, "cursor", open_cursor)
        monkeypatch.setattr(ap_text_import, "load_aliases", lambda: {})

    return install


def _workout(name, equipment):
    return APWorkout(
        "Synthetic AP resolver", datetime(2098, 4, 3, 17, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        45, (APExercise(name, equipment, "reps", (APSet(1, 20, 8),)),),
    )


def test_import_reuses_same_name_when_existing_exercise_has_no_equipment(synthetic_exercise_rows):
    rows = [synthetic_exercise_rows[0]]

    canonical, equipment, matched = _canonical_exercise(
        _exercise("Curls", "Dumbbells"), {}, rows,
    )

    assert (canonical, equipment, matched["id"]) == ("Curls", "dumbbell", 18)
    assert matched["equipment"] is None


def test_import_dry_run_counts_no_new_exercise_for_existing_unconfigured_name(
    monkeypatch, synthetic_exercise_rows, synthetic_import_database,
):
    existing = [synthetic_exercise_rows[0]]
    synthetic_import_database(existing)

    report = import_workouts([_workout("Curls", "Dumbbells")], dry_run=True)

    assert report.exercises_created == 0
    assert existing[0]["equipment"] is None


def test_import_prefers_existing_variant_for_exact_equipment(synthetic_exercise_rows):
    canonical, equipment, matched = _canonical_exercise(
        _exercise("Curls", "Dumbbells"), {}, synthetic_exercise_rows,
    )

    assert (canonical, equipment, matched["id"], matched["name"]) == (
        "Curls", "dumbbell", 23, "Curls (KH)",
    )


def test_import_resolves_full_equipment_alias_before_base_alias():
    exercise = APExercise(
        "Incline Press", "Smith machine", "reps", (APSet(1, 60, 8),),
    )
    aliases = {
        ap_text_import.normalize_alias("Incline Press · Smith machine"):
            ["Smith Incline Press"],
        ap_text_import.normalize_alias("Incline Press"):
            ["Generic Incline Press"],
    }
    rows = [
        {"id": 40, "name": "Incline Press", "equipment": "smith_machine",
         "unit": "reps", "load_mode": "external"},
        {"id": 41, "name": "Smith Incline Press", "equipment": "smith_machine",
         "unit": "reps", "load_mode": "external"},
        {"id": 42, "name": "Generic Incline Press", "equipment": "smith_machine",
         "unit": "reps", "load_mode": "external"},
    ]

    canonical, equipment, matched = _canonical_exercise(exercise, aliases, rows)

    assert (canonical, equipment, matched["id"]) == (
        "Smith Incline Press", "smith_machine", 41,
    )


def test_import_falls_back_to_base_alias_when_full_equipment_alias_is_missing():
    exercise = APExercise("Incline Press", "Smith machine", "reps", ())
    aliases = {
        ap_text_import.normalize_alias("Incline Press"): ["Generic Incline Press"],
    }
    rows = [
        {"id": 40, "name": "Incline Press", "equipment": "smith_machine",
         "unit": "reps", "load_mode": "external"},
        {"id": 42, "name": "Generic Incline Press", "equipment": "smith_machine",
         "unit": "reps", "load_mode": "external"},
    ]

    canonical, equipment, matched = _canonical_exercise(exercise, aliases, rows)

    assert (canonical, equipment, matched["id"]) == (
        "Generic Incline Press", "smith_machine", 42,
    )


def test_import_dry_run_counts_unknown_name_as_new(
    synthetic_import_database,
):
    name = "Plate Neck Curls"
    synthetic_import_database([])

    canonical, equipment, matched = _canonical_exercise(
        _exercise(name, "Cable"), {}, [],
    )
    report = import_workouts([_workout(name, "Cable")], dry_run=True)

    assert (canonical, equipment, matched) == (name, "cable", None)
    assert _variant_name(canonical, equipment, []) == name
    assert report.exercises_created == 1
    assert [(item.date.isoformat(), item.title) for item in report.new_workouts] == [
        ("2098-04-03", "Synthetic AP resolver"),
    ]


def test_import_detects_duplicate_even_if_time_and_title_differ(
    synthetic_import_database,
):
    exercise_name = "Synthetic Press"
    started_at = datetime(2098, 4, 3, 17, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    exercise_rows = [{
        "id": 18, "name": exercise_name, "equipment": None,
        "unit": "reps", "load_mode": "external",
    }]
    workout_rows = [
        {"workout_id": 7, "title": "Gym", "created_at": started_at,
         "exercise_id": 18, "exercise_name": exercise_name,
         "weight_kg": 60.0, "reps": 8},
        {"workout_id": 7, "title": "Gym", "created_at": started_at,
         "exercise_id": 18, "exercise_name": exercise_name,
         "weight_kg": 60.0, "reps": 8},
    ]
    synthetic_import_database(exercise_rows, workout_rows)
    ap_workout = APWorkout(
        "Synthetic AP plan", started_at.replace(minute=20), 45,
        (APExercise(exercise_name, None, "reps", (
            APSet(1, 60, 8), APSet(2, 60, 8),
        )),),
    )

    report = import_workouts([ap_workout], dry_run=True)

    assert (report.workouts_created, report.workouts_skipped) == (0, 1)
    assert [(item.title, item.existing_title) for item in report.skipped_workouts] == [
        ("Synthetic AP plan", "Gym"),
    ]
    assert report.unclear_workouts == ()


def test_import_reports_partial_set_overlap_as_unclear(synthetic_import_database):
    exercise_name = "Synthetic Bench Press"
    started_at = datetime(2098, 4, 3, 17, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    exercise_rows = [{
        "id": 18, "name": exercise_name, "equipment": None,
        "unit": "reps", "load_mode": "external",
    }]
    workout_rows = [{
        "workout_id": 7, "title": "Gym", "created_at": started_at,
        "exercise_id": 18, "weight_kg": 60.0, "reps": 8,
    }]
    synthetic_import_database(exercise_rows, workout_rows)
    ap_workout = APWorkout(
        "Synthetic AP plan", started_at.replace(minute=20), 45,
        (APExercise(exercise_name, None, "reps", (
            APSet(1, 60, 8), APSet(2, 62, 7), APSet(3, 64, 6),
        )),),
    )

    report = import_workouts([ap_workout], dry_run=True)

    assert (report.workouts_created, report.workouts_skipped) == (0, 0)
    assert report.new_workouts == ()
    assert [(item.title, item.existing_title) for item in report.unclear_workouts] == [
        ("Synthetic AP plan", "Gym"),
    ]


@pytest.mark.parametrize(
    ("existing_start", "expected_category"),
    (("18:50", "unclear"), ("18:51", "new")),
)
def test_import_uses_ninety_minute_boundary(
    synthetic_import_database, existing_start, expected_category,
):
    exercise_name = "Synthetic Boundary Press"
    existing_at = datetime.fromisoformat(
        f"2098-04-03T{existing_start}:00+02:00",
    )
    synthetic_import_database(
        [{"id": 18, "name": exercise_name, "equipment": None,
          "unit": "reps", "load_mode": "external"}],
        [{"workout_id": 7, "title": "Gym", "created_at": existing_at,
          "exercise_id": None, "weight_kg": None, "reps": None}],
    )
    ap_workout = APWorkout(
        "Synthetic AP plan",
        datetime(2098, 4, 3, 17, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        45,
        (APExercise(exercise_name, None, "reps", (APSet(1, 60, 8),)),),
    )

    report = import_workouts([ap_workout], dry_run=True)

    if expected_category == "unclear":
        assert [(item.title, item.existing_title) for item in report.unclear_workouts] == [
            ("Synthetic AP plan", "Gym"),
        ]
        assert report.new_workouts == ()
    else:
        assert [(item.title, item.existing_title) for item in report.new_workouts] == [
            ("Synthetic AP plan", None),
        ]
        assert report.unclear_workouts == ()


def test_import_skips_exact_title_on_same_date_without_time_or_sets(
    synthetic_import_database,
):
    ap_workout = APWorkout(
        "Same title", datetime(2098, 4, 3, 17, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        45, (),
    )
    synthetic_import_database(
        [],
        [{"workout_id": 7, "title": "Same title", "created_at": None,
          "exercise_id": None, "weight_kg": None, "reps": None}],
    )

    report = import_workouts([ap_workout], dry_run=True)

    assert (report.workouts_created, report.workouts_skipped) == (0, 1)
    assert [(item.title, item.existing_title) for item in report.skipped_workouts] == [
        ("Same title", "Same title"),
    ]


def test_dry_run_detects_duplicates_inside_the_same_import(synthetic_import_database):
    synthetic_import_database([])
    workout = _workout("Synthetic Press", "Cable")

    report = import_workouts([workout, workout], dry_run=True)

    assert (report.workouts_created, report.workouts_skipped) == (1, 1)
    assert [(item.title, item.existing_title) for item in report.new_workouts] == [
        ("Synthetic AP resolver", None),
    ]
    assert [(item.title, item.existing_title) for item in report.skipped_workouts] == [
        ("Synthetic AP resolver", "Synthetic AP resolver"),
    ]


@pytest.mark.parametrize(
    ("label", "code"),
    (
        ("Cable", "cable"),
        ("Machine", "machine"),
        ("Smith machine", "smith_machine"),
        ("Dumbbells", "dumbbell"),
        ("Barbell", "barbell"),
        ("Kettlebells", "kettlebell"),
        ("Bodyweight", "bodyweight"),
    ),
)
def test_import_maps_alpha_progression_equipment_labels(label, code):
    assert _equipment_code(label) == code


def test_parser_reads_strength_bodyweight_and_timed_set_rows():
    workout = parse_export(FIXTURE.read_text(encoding="utf-8"))[0]

    assert workout.title == "Synthetic Upper A · Day 1 · Week 1"
    assert workout.started_at == datetime(
        2098, 4, 3, 17, 20, tzinfo=ZoneInfo("Europe/Berlin"),
    )
    assert workout.duration_min == 62
    assert len(workout.exercises) == 9

    smith, dumbbells, pullups, band, hold, row, plank, squat, bodyweight = workout.exercises
    assert (smith.name, smith.equipment, smith.unit) == (
        "Incline Bench Press", "Smith machine", "reps",
    )
    assert smith.note == "Synthetic note: smooth tempo"
    assert [
        (item.set_index, item.weight_kg, item.reps) for item in smith.sets
    ] == [(1, 80.5, 8), (2, 10.0, 6), (3, -20.0, 5)]
    assert smith.load_mode == "added"
    assert dumbbells.sets[0].weight_kg == 24.5
    assert [(item.weight_kg, item.reps) for item in pullups.sets] == [
        (0.0, 9), (10.0, 6),
    ]
    assert pullups.load_mode == "added"
    assert (band.unit, band.sets[0].weight_kg, band.sets[0].reps) == (
        "reps", 15.5, 12,
    )
    assert (hold.unit, hold.sets[0].duration_s) == ("min", 35)
    assert (row.unit, row.sets[0].distance_km, row.sets[0].duration_s) == (
        "km", 2.5, 750,
    )
    assert (plank.unit, plank.sets[0].duration_s) == ("min", 90)
    assert (squat.unit, squat.sets[0].weight_kg, squat.sets[0].reps) == (
        "reps", 0.0, 15,
    )
    assert squat.load_mode == "bodyweight"
    assert (bodyweight.load_mode, bodyweight.sets[0].weight_kg) == ("bodyweight", 0.0)


@pytest.mark.parametrize(
    ("duration", "expected_minutes"),
    (("1 hr", 60), ("46 min", 46), ("1:02 hr", 62)),
)
def test_parser_converts_workout_duration_variants(duration, expected_minutes):
    text = f'"Synthetic workout";"2098-04-03 17:20 h";"{duration}"'

    assert parse_export(text)[0].duration_min == expected_minutes


def test_parser_accepts_single_digit_hour_in_workout_start_time():
    workout = parse_export('"Synthetic workout";"2098-04-03 9:05 h";"46 min"')[0]

    assert workout.started_at == datetime(
        2098, 4, 3, 9, 5, tzinfo=ZoneInfo("Europe/Berlin"),
    )


def test_import_cli_prints_new_skipped_and_unclear_workouts(
    tmp_path, monkeypatch, capsys,
):
    export_path = tmp_path / "synthetic.txt"
    export_path.write_text("synthetic", encoding="utf-8")
    report = ImportReport(
        workouts_created=1, workouts_skipped=1, exercises_created=0,
        sets_created=0, dry_run=True,
        new_workouts=(WorkoutReportEntry(date(2098, 4, 3), "New AP workout"),),
        skipped_workouts=(WorkoutReportEntry(
            date(2098, 4, 3), "AP plan", "Gym",
        ),),
        unclear_workouts=(WorkoutReportEntry(
            date(2098, 4, 3), "Maybe AP plan", "Evening session",
        ),),
    )
    monkeypatch.setattr(import_ap_text, "parse_export", lambda _text: ["parsed"])
    monkeypatch.setattr(
        import_ap_text, "import_workouts", lambda _workouts, dry_run: report,
    )

    assert import_ap_text.main([str(export_path), "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "Dry-run: 1 neue Workouts, 1 übersprungen, 1 unklar" in output
    assert "Neu: 2098-04-03 — New AP workout" in output
    assert "Übersprungen: 2098-04-03 — AP plan ↔ Gym" in output
    assert "Unklar: 2098-04-03 — Maybe AP plan ↔ Evening session" in output


@pytest.fixture
def ap_import_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test in dieser Sandbox nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()


@pytest.mark.db
def test_import_canonicalizes_equipment_variants_and_is_idempotent(ap_import_database):
    token = uuid4().hex
    title = f"Synthetic AP import {token}"
    variant_name = f"Synthetic {token} Equipment Exercise"
    unknown_name = f"Synthetic {token} Band Press"
    hold_name = f"Synthetic {token} Tempo Hold"
    row_name = f"Synthetic {token} Rowing"
    plank_name = f"Synthetic {token} Plank"
    text = (
        f'"{title}";"2098-04-03 17:20 h";"46 min"\n'
        f'"1. {variant_name} · Smith machine · 8 reps";"Synthetic note"\n'
        "#;KG;REPS\n1;80,5;8\n2;+0;6\n"
        f'"2. {variant_name} · Dumbbells · 8 reps"\n'
        "#;KG;REPS\n1;24,5;8\n"
        '"3. Klimmzüge · Bodyweight · 10 reps"\n'
        "#;KG;REPS\n1;+0;9\n2;+10;6\n"
        f'"4. {unknown_name} · Resistance Band · 12 reps"\n'
        "#;KG;REPS\n1;15,5;12\n"
        f'"5. {hold_name} · Dumbbells"\n'
        "#;KG;SECS\n1;12,5;0:35\n"
        f'"6. {row_name} · Row Erg"\n'
        "#;KM;MINS\n1;2,5;12,5\n"
        f'"7. {plank_name} · Bodyweight"\n'
        "#;MINS\n1;1:30\n"
    )
    before_ids = {row["id"] for row in db.query("SELECT id FROM exercises")}
    workout_id = None
    try:
        report = import_workouts(parse_export(text))
        assert (report.workouts_created, report.workouts_skipped) == (1, 0)
        assert report.exercises_created >= 1

        workout = db.query_one(
            "SELECT id,title,date,duration_min,notes FROM workouts WHERE title=%s",
            (title,),
        )
        workout_id = workout["id"]
        assert (workout["date"].isoformat(), workout["duration_min"]) == (
            "2098-04-03", 46,
        )
        assert workout["notes"] == f"{variant_name}: Synthetic note"
        rows = db.query(
            """SELECT e.id,e.name,e.equipment,e.unit,e.load_mode,ws.set_index,ws.weight_kg,
                      ws.reps,ws.distance_km,ws.duration_s
               FROM workout_sets ws JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s ORDER BY e.name,ws.set_index""",
            (workout_id,),
        )
        variant_rows = [row for row in rows if row["name"] == variant_name]
        assert len({row["id"] for row in variant_rows}) == 1
        assert {row["equipment"] for row in variant_rows} == {"smith_machine"}
        unknown_row = next(row for row in rows if row["name"] == unknown_name)
        assert db.query_one(
            "SELECT id,equipment,unit FROM exercises WHERE name=%s", (unknown_name,),
        ) == {
            "id": unknown_row["id"], "equipment": "resistance_band", "unit": "reps",
        }
        imported_row = next(row for row in rows if row["name"] == row_name)
        assert (imported_row["unit"], imported_row["distance_km"], imported_row["duration_s"]) == (
            "km", 2.5, 750,
        )
        imported_hold = next(row for row in rows if row["name"] == hold_name)
        assert (imported_hold["unit"], imported_hold["weight_kg"], imported_hold["duration_s"]) == (
            "min", 12.5, 35,
        )
        imported_plank = next(row for row in rows if row["name"] == plank_name)
        assert (imported_plank["unit"], imported_plank["duration_s"]) == ("min", 90)
        assert [(row["weight_kg"], row["reps"]) for row in rows if row["reps"] == 9] == [
            (0.0, 9),
        ]

        repeated = import_workouts(parse_export(text))
        assert (repeated.workouts_created, repeated.workouts_skipped) == (0, 1)
        assert db.query_one("SELECT COUNT(*) AS n FROM workouts WHERE title=%s", (title,))["n"] == 1
    finally:
        if workout_id is not None:
            db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        new_ids = [
            row["id"] for row in db.query("SELECT id FROM exercises")
            if row["id"] not in before_ids
        ]
        if new_ids:
            db.execute("DELETE FROM exercises WHERE id=ANY(%s)", (new_ids,))


@pytest.mark.db
def test_dry_run_counts_without_writing_rows(ap_import_database):
    token = uuid4().hex
    title = f"Synthetic AP dry run {token}"
    name = f"Synthetic {token} bodyweight hold"
    text = (
        f'"{title}";"2098-04-04 17:20 h";"1 hr"\n'
        f'"1. {name} · Bodyweight"\n#;MINS\n1;1:30\n'
    )

    report = import_workouts(parse_export(text), dry_run=True)

    assert (report.workouts_created, report.workouts_skipped) == (1, 0)
    assert report.exercises_created == 1
    assert [(item.date.isoformat(), item.title) for item in report.new_workouts] == [
        ("2098-04-04", title),
    ]
    assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    assert db.query_one("SELECT id FROM exercises WHERE name=%s", (name,)) is None


@pytest.mark.db
def test_import_marks_similar_time_workout_without_matching_sets_unclear(ap_import_database):
    token = uuid4().hex
    title = f"Synthetic AP duplicate {token}"
    existing_title = f"Synthetic earlier workout {token}"
    started_at = datetime(2098, 4, 5, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    workout_id = db.insert_returning(
        """INSERT INTO workouts (date,title,type,created_at)
           VALUES (%s,%s,'strength',%s) RETURNING id""",
        (started_at.date(), existing_title, started_at),
    )
    text = (
        f'"{title}";"2098-04-05 18:30 h";"1 hr"\n'
        f'"1. Synthetic {token} Exercise · Bodyweight"\n#;REPS\n1;10\n'
    )
    try:
        report = import_workouts(parse_export(text))
        assert (report.workouts_created, report.workouts_skipped) == (0, 0)
        assert [(item.title, item.existing_title) for item in report.unclear_workouts] == [
            (title, existing_title),
        ]
        assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))


@pytest.mark.db
def test_import_duplicate_check_skips_same_sets_within_ninety_minutes(ap_import_database):
    token = uuid4().hex
    title = f"Synthetic AP duplicate check {token}"
    existing_title = "Gym"
    exercise_name = f"Synthetic {token} Incline Press"
    workout_date = datetime(2098, 4, 5, 17, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,unit,equipment,load_mode)
           VALUES (%s,'strength','reps',NULL,'external') RETURNING id""",
        (exercise_name,),
    )
    workout_id = db.insert_returning(
        """INSERT INTO workouts (date,title,type,created_at)
           VALUES (%s,%s,'strength',%s) RETURNING id""",
        (workout_date.date(), existing_title, workout_date),
    )
    for set_index in (1, 2):
        db.execute(
            """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg)
               VALUES (%s,%s,%s,8,60)""",
            (workout_id, exercise_id, set_index),
        )
    ap_workout = APWorkout(
        title,
        datetime(2098, 4, 5, 17, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        45,
        (APExercise(exercise_name, None, "reps", (
            APSet(1, 60, 8), APSet(2, 60, 8),
        )),),
    )

    try:
        report = import_workouts([ap_workout])

        assert (report.workouts_created, report.workouts_skipped) == (0, 1)
        assert [(item.title, item.existing_title) for item in report.skipped_workouts] == [
            (title, existing_title),
        ]
        assert report.unclear_workouts == ()
        assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


@pytest.mark.db
def test_import_marks_less_than_half_matching_sets_unclear(ap_import_database):
    token = uuid4().hex
    title = f"Synthetic AP partial overlap {token}"
    existing_title = "Gym"
    exercise_name = f"Synthetic {token} Bench Press"
    workout_date = datetime(2098, 4, 6, 17, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    exercise_id = db.insert_returning(
        """INSERT INTO exercises (name,category,unit,equipment,load_mode)
           VALUES (%s,'strength','reps',NULL,'external') RETURNING id""",
        (exercise_name,),
    )
    workout_id = db.insert_returning(
        """INSERT INTO workouts (date,title,type,created_at)
           VALUES (%s,%s,'strength',%s) RETURNING id""",
        (workout_date.date(), existing_title, workout_date),
    )
    db.execute(
        """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg)
           VALUES (%s,%s,1,8,60)""",
        (workout_id, exercise_id),
    )
    ap_workout = APWorkout(
        title,
        datetime(2098, 4, 6, 17, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        45,
        (APExercise(exercise_name, None, "reps", (
            APSet(1, 60, 8), APSet(2, 62, 7), APSet(3, 64, 6),
        )),),
    )

    try:
        report = import_workouts([ap_workout], dry_run=True)

        assert (report.workouts_created, report.workouts_skipped) == (0, 0)
        assert [(item.title, item.existing_title) for item in report.unclear_workouts] == [
            (title, existing_title),
        ]
        assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))
