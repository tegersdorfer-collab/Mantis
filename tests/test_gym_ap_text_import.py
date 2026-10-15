"""Synthetische AlphaProgression-Textimporte."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
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
    _canonical_exercise,
    _equipment_code,
    _variant_name,
    import_workouts,
    parse_export,
)


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
    def install(exercise_rows):
        class Cursor:
            def execute(self, sql, _params=()):
                self.sql = sql
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                    raise AssertionError("Dry-run darf keine Datenbankzeilen schreiben")

            def fetchall(self):
                if "FROM exercises" in self.sql:
                    return [dict(row) for row in exercise_rows]
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
    assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    assert db.query_one("SELECT id FROM exercises WHERE name=%s", (name,)) is None


@pytest.mark.db
def test_import_skips_existing_workout_with_same_local_start_time(ap_import_database):
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
        assert (report.workouts_created, report.workouts_skipped) == (0, 1)
        assert db.query_one("SELECT id FROM workouts WHERE title=%s", (title,)) is None
    finally:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
