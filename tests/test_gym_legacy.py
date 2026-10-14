"""Alias-Zuordnung auf die englischen Bestandsübungen (AlphaProgression-Import)."""

from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from core import db
from domains.gym import coach as coach_module
from domains.gym import session
from domains.gym.aliases import is_pullup_name, load_aliases, normalize_alias
from domains.gym.coach import GymCoach
from domains.gym.legacy import LEGACY_EXERCISES
from domains.gym.parser import parse_set_message


def _legacy_db(monkeypatch, names, equipment=None):
    rows = [
        {"exercise_id": index, "alias": name, "exercise": name,
         "equipment": (equipment or {}).get(name)}
        for index, name in enumerate(names, 1)
    ]

    def query(sql, _params=()):
        return [] if "exercise_aliases" in sql else rows

    monkeypatch.setattr(db, "query", query)


ALL_NAMES = [item.name for item in LEGACY_EXERCISES]


def test_legacy_table_covers_the_most_used_stock_exercises():
    top = [
        "Incline Bench Press", "Bench Press", "Lateral Raises", "Curls",
        "Reverse Butterfly with Wide Grip", "Triceps Pushdowns", "Overhead Triceps Extensions",
        "Butterfly with Wide Grip", "Seated Shoulder Press", "Dips",
        "Pull-Ups with Wide Overhand Grip", "Face Pulls with Rope", "Shoulder Press",
        "Kneeling Crunches", "Hammer Curls", "Leg Extensions", "Leg Press",
        "Seated Calf Raises", "Standing Calf Raises", "Reverse Curls", "Shrugs",
        "Seated Leg Curls", "Bent-Over Rows", "Squats", "Lat Pulldowns with Wide Overhand Grip",
        "High Cable Flys", "Incline Curls", "Hip Adduction", "Hanging Leg Raises",
        "Lat Pulldowns with Close Neutral Grip", "Romanian Deadlifts", "Skull Crushers",
        "Hack Squats", "Deadlifts", "Lying Leg Curls", "Hip Abduction", "Chest Press",
        "Crunch Machine", "Preacher Curls", "Overhead Triceps Extensions with Rope",
    ]
    assert len(top) >= 40
    assert set(top) <= set(ALL_NAMES)


def test_legacy_alias_keys_are_unique_and_never_start_with_a_parser_prefix():
    seen = {}
    for item in LEGACY_EXERCISES:
        for alias in (item.name, *item.aliases):
            key = normalize_alias(alias)
            assert seen.setdefault(key, item.name) == item.name, alias
            assert not key.split()[0] in {"kh", "lh", "sz", "kabel", "maschine"}, alias


@pytest.mark.parametrize(("typed", "stock"), [
    ("Bank", "Bench Press"), ("Bankdrücken", "Bench Press"), ("BD", "Bench Press"),
    ("Bench", "Bench Press"), ("Schrägbank", "Incline Bench Press"),
    ("Incline", "Incline Bench Press"), ("Seitheben", "Lateral Raises"),
    ("Curls", "Curls"), ("Bizepscurls", "Curls"), ("Hammercurls", "Hammer Curls"),
    ("Trizepsdrücken", "Triceps Pushdowns"), ("Pushdowns", "Triceps Pushdowns"),
    ("Überkopf Trizeps", "Overhead Triceps Extensions"),
    ("Butterfly", "Butterfly with Wide Grip"),
    ("Reverse Butterfly", "Reverse Butterfly with Wide Grip"),
    ("Schulterdrücken", "Shoulder Press"), ("Schulterdrücken sitzend", "Seated Shoulder Press"),
    ("Dips", "Dips"), ("Klimmzüge", "Pull-Ups with Wide Overhand Grip"),
    ("Face Pulls", "Face Pulls with Rope"), ("Beinpresse", "Leg Press"),
    ("Beinstrecker", "Leg Extensions"), ("Wadenheben sitzend", "Seated Calf Raises"),
    ("Wadenheben stehend", "Standing Calf Raises"), ("Crunches", "Kneeling Crunches"),
    ("Reverse Curls", "Reverse Curls"), ("Kniebeuge", "Squats"), ("Kreuzheben", "Deadlifts"),
    ("Latzug", "Lat Pulldowns with Wide Overhand Grip"),
])
def test_german_names_and_abbreviations_resolve_to_the_stock_name(monkeypatch, typed, stock):
    _legacy_db(monkeypatch, ALL_NAMES)

    assert load_aliases()[normalize_alias(typed)] == [stock]


def test_stock_alias_is_only_used_when_that_exercise_exists(monkeypatch):
    _legacy_db(monkeypatch, ["Leg Press"])

    aliases = load_aliases()

    assert aliases["bank"] == ["Bankdrücken"]
    assert aliases["beinpresse"] == ["Leg Press"]


def test_bank_log_uses_the_stock_bench_press(monkeypatch):
    _legacy_db(monkeypatch, ALL_NAMES + ["Bankdrücken"], {"Bench Press": "barbell"})

    parsed = parse_set_message("Bank 80 8 8 7", load_aliases())

    assert parsed.exercise == "Bench Press"
    assert parsed.known is True and parsed.ambiguous == ()
    assert [(item.weight_kg, item.reps) for item in parsed.sets] == [(80, 8), (80, 8), (80, 7)]


def test_stock_pullup_name_keeps_bodyweight_and_added_load_rules(monkeypatch):
    _legacy_db(monkeypatch, ALL_NAMES)
    aliases = load_aliases()

    bodyweight = parse_set_message("Klimmzüge 8 7 6", aliases)
    added = parse_set_message("Klimmzüge +10 8 7", aliases)

    assert bodyweight.exercise == "Pull-Ups with Wide Overhand Grip"
    assert bodyweight.load_mode == "bodyweight"
    assert [item.weight_kg for item in bodyweight.sets] == [None] * 3
    assert added.load_mode == "added"
    assert is_pullup_name("Pull-Ups with Close Neutral Grip")
    assert not is_pullup_name("Assisted Pull-Ups with Wide Grip")


def test_stock_curls_do_not_trigger_the_equipment_question(monkeypatch):
    _legacy_db(monkeypatch, ALL_NAMES)
    monkeypatch.setattr(coach_module, "load_aliases", load_aliases)

    intent = GymCoach().classify("Curls 15 10 10 8")
    hammer = GymCoach().classify("Hammercurls 14 10 10")

    assert intent.action == "set_logged" and intent.parsed.exercise == "Curls"
    assert hammer.action == "set_logged" and hammer.parsed.exercise == "Hammer Curls"


def test_bare_bank_asks_when_a_second_variant_exists(monkeypatch):
    _legacy_db(monkeypatch, ["Bankdrücken", "KH Bankdrücken"])

    aliases = load_aliases()
    parsed = parse_set_message("Bank 80 8", aliases)

    assert parsed.exercise is None
    assert set(parsed.ambiguous) == {"Bankdrücken", "KH Bankdrücken"}


def test_single_stored_exercise_with_equipment_keeps_its_real_name(monkeypatch):
    _legacy_db(monkeypatch, ["Bankdrücken"], {"Bankdrücken": "barbell"})

    assert load_aliases()["bank"] == ["Bankdrücken"]


# ── Datenbank: synthetische Bestandsdaten im Stil des Imports ────────────────

@pytest.fixture
def stock_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test in dieser Sandbox nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()
    if session.open_session():
        pytest.skip("mantis_test enthält bereits eine offene Gym-Session")
    created = {"exercises": [], "workouts": []}
    yield created
    for workout_id in created["workouts"]:
        db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
    for exercise_id in created["exercises"]:
        db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


def _stock_exercise(created, name):
    row = db.query_one("SELECT id FROM exercises WHERE name=%s", (name,))
    if row:
        if db.query_one("SELECT 1 AS n FROM workout_sets WHERE exercise_id=%s LIMIT 1", (row["id"],)):
            pytest.skip(f"mantis_test enthält schon Sätze für {name}")
        return row["id"]
    exercise_id = db.insert_returning(
        "INSERT INTO exercises (name, category, muscle, unit) "
        "VALUES (%s,'strength','chest','reps') RETURNING id",
        (name,),
    )
    created["exercises"].append(exercise_id)
    return exercise_id


def _stock_workout(created, exercise_id, day, sets, *, workout_type="strength"):
    workout_id = db.insert_returning(
        "INSERT INTO workouts (date, title, type, notes) "
        "VALUES (%s,%s,%s,'synthetic stock fixture') RETURNING id",
        (day, f"stock {uuid4()}", workout_type),
    )
    created["workouts"].append(workout_id)
    for index, (weight, reps) in enumerate(sets, 1):
        db.execute(
            "INSERT INTO workout_sets (workout_id, exercise_id, set_index, reps, weight_kg) "
            "VALUES (%s,%s,%s,%s,%s)",
            (workout_id, exercise_id, index, reps, weight),
        )
    return workout_id


@pytest.mark.db
def test_bank_log_continues_the_stock_bench_press_history(stock_database):
    bench = _stock_exercise(stock_database, "Bench Press")
    _stock_workout(stock_database, bench, date(2026, 9, 1), [(77.5, 8), (77.5, 8), (77.5, 7)])
    _stock_workout(stock_database, bench, date(2026, 9, 8), [(80, 8), (80, 8), (80, 8)])
    before = db.query_one("SELECT COUNT(*) AS n FROM exercises")["n"]
    moment = datetime.now(ZoneInfo("Europe/Berlin"))

    parsed = parse_set_message("Bank 80 8 8 7", load_aliases())
    result = session.add_sets(parsed, allow_create=parsed.known, now=moment)
    stock_database["workouts"].append(result["workout_id"])
    used = db.query_one(
        "SELECT DISTINCT exercise_id FROM workout_sets WHERE workout_id=%s",
        (result["workout_id"],),
    )

    assert parsed.exercise == "Bench Press"
    assert used["exercise_id"] == bench
    assert db.query_one("SELECT COUNT(*) AS n FROM exercises")["n"] == before
    assert db.query_one("SELECT id FROM exercises WHERE name='Bankdrücken'") is None
    assert [(r["weight_kg"], r["reps"]) for r in result["previous_sets"]] == [(80, 8)] * 3
    assert result["progression"]["target_reps"] == [8, 8, 8]
    assert result["progression"]["weight_kg"] == 80
    assert not any(result["records"].values())


@pytest.mark.db
def test_bank_log_flags_a_pr_against_the_stock_history(stock_database):
    bench = _stock_exercise(stock_database, "Bench Press")
    _stock_workout(stock_database, bench, date(2026, 9, 1), [(80, 8), (80, 8), (80, 8)])
    moment = datetime.now(ZoneInfo("Europe/Berlin"))

    parsed = parse_set_message("Bank 85 6 6", load_aliases())
    result = session.add_sets(parsed, allow_create=True, now=moment)
    stock_database["workouts"].append(result["workout_id"])

    assert result["exercise"] == "Bench Press"
    assert result["records"]["weight"] is True
    assert "🏆" in GymCoach._log_reply(result)


@pytest.mark.db
def test_bank_verlauf_shows_the_stock_history(stock_database):
    bench = _stock_exercise(stock_database, "Bench Press")
    _stock_workout(stock_database, bench, date(2026, 9, 1), [(77.5, 8), (77.5, 8)])
    _stock_workout(stock_database, bench, date(2026, 9, 8), [(80, 8), (80, 7)])
    monkey_aliases = load_aliases()

    intent = GymCoach().classify("Bank Verlauf")
    reply = GymCoach._history(intent.payload["exercise"])

    assert monkey_aliases["bank"] == ["Bench Press"]
    assert intent.action == "history" and intent.payload["exercise"] == "Bench Press"
    assert "08.09." in reply.text and "01.09." in reply.text
    assert "80 kg×8" in reply.text
    assert "noch keinen" not in reply.text


@pytest.mark.db
def test_history_orders_stock_workouts_by_date_not_import_time(stock_database):
    bench = _stock_exercise(stock_database, "Bench Press")
    # Import-Reihenfolge verkehrt: das neuere Datum bekommt die kleinere ID.
    _stock_workout(stock_database, bench, date(2026, 9, 8), [(80, 8)])
    _stock_workout(stock_database, bench, date(2026, 9, 1), [(70, 8)])

    history = session.exercise_history("Bench Press", limit=2)

    assert [item["sets"][0]["weight_kg"] for item in history] == [80, 70]


@pytest.mark.db
def test_backfill_fills_missing_stock_metadata_without_touching_rows(stock_database):
    bench = _stock_exercise(stock_database, "Bench Press")
    pullups = _stock_exercise(stock_database, "Pull-Ups with Wide Overhand Grip")
    db.execute(
        "UPDATE exercises SET equipment=NULL, exercise_type=NULL, load_mode=NULL WHERE id IN (%s,%s)",
        (bench, pullups),
    )
    custom = _stock_exercise(stock_database, "Curls")
    db.execute("UPDATE exercises SET exercise_type='compound' WHERE id=%s", (custom,))
    before = db.query_one("SELECT name, category, muscle, unit FROM exercises WHERE id=%s", (bench,))

    db.run_migrations()
    db.run_migrations()

    bench_row = db.query_one("SELECT * FROM exercises WHERE id=%s", (bench,))
    assert bench_row["equipment"] == "barbell"
    assert bench_row["exercise_type"] == "compound"
    assert db.query_one("SELECT load_mode FROM exercises WHERE id=%s", (pullups,))["load_mode"] == "added"
    assert db.query_one("SELECT exercise_type FROM exercises WHERE id=%s", (custom,))["exercise_type"] == "compound"
    assert {k: bench_row[k] for k in before} == before
    assert db.query_one("SELECT equipment FROM exercises WHERE id=%s", (custom,))["equipment"] is None


@pytest.mark.db
def test_variant_name_from_the_choice_list_finds_the_stored_history(stock_database):
    name = f"Bankdrücken Synthetic {uuid4().hex[:6]}"
    plain = db.insert_returning(
        "INSERT INTO exercises (name, category, muscle, unit, equipment) "
        "VALUES (%s,'strength','chest','reps','barbell') RETURNING id",
        (name,),
    )
    other = db.insert_returning(
        "INSERT INTO exercises (name, category, muscle, unit) "
        "VALUES (%s,'strength','chest','reps') RETURNING id",
        (f"KH {name}",),
    )
    stock_database["exercises"] += [plain, other]
    _stock_workout(stock_database, plain, date(2026, 9, 1), [(80, 8)])

    history = session.exercise_history(f"LH {name}")

    assert [row["sets"][0]["weight_kg"] for row in history] == [80]
    assert session.exercise_history(f"KH {name}") == []


@pytest.mark.db
def test_finish_doubles_dumbbell_volume_for_stock_names_without_metadata(stock_database):
    name = f"KH Synthetic Press {uuid4().hex[:6]}"
    exercise_id = db.insert_returning(
        "INSERT INTO exercises (name, category, muscle, unit) "
        "VALUES (%s,'strength','shoulders','reps') RETURNING id",
        (name,),
    )
    stock_database["exercises"].append(exercise_id)
    moment = datetime.now(ZoneInfo("Europe/Berlin"))

    parsed = parse_set_message(
        "KH Synthetic Press 22,5 10",
        {**load_aliases(), "synthetic press": [name]},
    )
    result = session.add_sets(parsed, allow_create=False, now=moment)
    stock_database["workouts"].append(result["workout_id"])
    summary = session.finish(moment + timedelta(minutes=10))

    assert parsed.exercise is not None
    assert summary["volume_kg"] == 450
