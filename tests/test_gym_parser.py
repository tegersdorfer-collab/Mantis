"""Realistische Freitextfälle des deterministischen Gym-Parsers."""

import pytest
from time import perf_counter

from core import db
from domains.gym.aliases import load_aliases
from domains.gym.parser import parse_set_message


@pytest.mark.parametrize(
    ("text", "exercise", "weights", "reps", "equipment"),
    [
        ("Bank 80 8 8 7", "Bankdrücken", [80, 80, 80], [8, 8, 7], None),
        ("bankdrücken 80kg 3x8", "Bankdrücken", [80] * 3, [8] * 3, None),
        ("Bankdruecken 80 kg 3×8", "Bankdrücken", [80] * 3, [8] * 3, None),
        ("BD 80 8 8 8", "Bankdrücken", [80] * 3, [8] * 3, None),
        ("Bench 80kg 3x8", "Bankdrücken", [80] * 3, [8] * 3, None),
        ("Bench Press 80 8, 8, 7", "Bankdrücken", [80] * 3, [8, 8, 7], None),
        ("Squat 100 5x5", "Kniebeuge", [100] * 5, [5] * 5, None),
        ("Kniebeuge 100kg 5×5", "Kniebeuge", [100] * 5, [5] * 5, None),
        ("LH Kniebeuge 100 5 5 5", "Kniebeuge", [100] * 3, [5] * 3, "barbell"),
        ("Beinpresse 140 12 12", "Beinpresse", [140] * 2, [12, 12], None),
        ("Leg Press 140kg 2x12", "Beinpresse", [140] * 2, [12, 12], None),
        ("KH Schulterdrücken 22,5 10 10 9", "Schulterdrücken", [22.5] * 3, [10, 10, 9], "dumbbell"),
        ("Kurzhantel-Schulterdrücken 22.5kg 3 x 10", "Schulterdrücken", [22.5] * 3, [10] * 3, "dumbbell"),
        ("Shoulder Press 40 10x3", "Schulterdrücken", [40] * 10, [3] * 10, None),
        ("OHP 40kg 3x8", "Schulterdrücken", [40] * 3, [8] * 3, None),
        ("Klimmzüge +10 8 7", "Klimmzüge", [10, 10], [8, 7], None),
        ("Pull-up +10kg 2x8", "Klimmzüge", [10, 10], [8, 8], None),
        ("Klimmzüge 8 7 6", "Klimmzüge", [None] * 3, [8, 7, 6], None),
        ("Bank 80 8, 82.5 6", "Bankdrücken", [80, 82.5], [8, 6], None),
        ("Bank 80,5 8", "Bankdrücken", [80.5], [8], None),
        ("bank 80,5kg 8 8", "Bankdrücken", [80.5] * 2, [8, 8], None),
        ("  BANKDRÜCKEN   80 KG   8  7  ", "Bankdrücken", [80] * 2, [8, 7], None),
        ("Bank 80kg x 8", "Bankdrücken", [80], [8], None),
        ("Bank 80 kg 3 Sätze à 8 Wdh", "Bankdrücken", [80] * 3, [8] * 3, None),
        ("SZ-Stange Bizepscurls 25 12 10", "Bizepscurls", [25, 25], [12, 10], "ez_bar"),
        ("LH Bankdrücken 60 10 9", "Bankdrücken", [60, 60], [10, 9], "barbell"),
        ("KH Bankdrücken 24 10 9", "Bankdrücken", [24, 24], [10, 9], "dumbbell"),
        ("Langhantel Kniebeugen 90.5 5 5", "Kniebeuge", [90.5] * 2, [5, 5], "barbell"),
        ("Klimmzüge + 5,5 6 5", "Klimmzüge", [5.5, 5.5], [6, 5], None),
        ("Beinpresse 1.000 10", "Beinpresse", [1000], [10], None),
        ("Neue Übung 42 10 9", "Neue Übung", [42, 42], [10, 9], None),
        ("Fliegende am Kabel 12.5kg 12 11", "Fliegende am Kabel", [12.5] * 2, [12, 11], None),
    ],
)
def test_parses_realistic_training_lines(text, exercise, weights, reps, equipment):
    parsed = parse_set_message(text)

    assert parsed is not None
    assert parsed.exercise == exercise
    assert [item.weight_kg for item in parsed.sets] == weights
    assert [item.reps for item in parsed.sets] == reps
    assert parsed.equipment == equipment


@pytest.mark.parametrize(
    "text",
    [
        "Ich bin 8 Stunden gelaufen",
        "Heute 80 8 8",
        "Meine Bestellung 80 8 8",
        "Die Zahl 80 und 8 sind Glückszahlen",
        "80 kg wie viel sind das in pounds?",
        "Heute 80 kg, ganz schön schwer",
        "Ich trainiere morgen vielleicht",
        "Training war super, danke!",
        "Push war gestern anstrengend",
        "Was mach ich heute?",
        "8 8 8",
        "80 8 8",
        "Bank 80",
        "Bank acht Wiederholungen",
        "Bank 0 8 8",
        "Bank 80 0 8",
        "Bank 80 31 8",
        "Bank 80kg 3x0",
        "Bank 80kg 0x8",
        "Bank 80kg 3 mal",
        "Bank 80 kg bitte in pounds umrechnen",
        "Laufband 80 8 8",
        "Kalorien 500 8 8",
        "Ich wiege 80 kg und bin 8 Stunden gelaufen",
        "Wieviel sind 80 kg in lb?",
        "Bank 80, 82.5 6",
        "Bank 80 8,82.5 6",
        "Bank 80kg x 8x8x8",
        "Bank 80kg 3x8x2",
    ],
)
def test_rejects_non_training_and_malformed_lines(text):
    assert parse_set_message(text) is None


def test_unique_typo_resolves_and_preserves_original_alias():
    parsed = parse_set_message("Bankdrüken 80 8 8")

    assert parsed is not None
    assert parsed.exercise == "Bankdrücken"
    assert parsed.alias == "Bankdrüken"
    assert parsed.fuzzy is True
    assert parsed.known is True


def test_ambiguous_alias_is_not_guessed():
    parsed = parse_set_message(
        "Press 40 8 8",
        aliases={"press": ["Bankdrücken", "Schulterdrücken"]},
    )

    assert parsed is not None
    assert parsed.exercise is None
    assert parsed.ambiguous == ("Bankdrücken", "Schulterdrücken")


def test_unfamiliar_exercise_is_parsed_but_never_assumed_known():
    parsed = parse_set_message("Cable Fly 12.5 12 10")

    assert parsed is not None
    assert parsed.exercise == "Cable Fly"
    assert parsed.known is False


def test_database_pullup_name_keeps_bodyweight_mode():
    parsed = parse_set_message("Pull-up 8 7", aliases={"pull up": ["Pull Up"]})

    assert parsed is not None
    assert parsed.exercise == "Pull Up"
    assert [item.weight_kg for item in parsed.sets] == [None, None]
    assert parsed.load_mode == "bodyweight"


def test_database_plural_pullup_name_keeps_bodyweight_mode():
    parsed = parse_set_message("Pull-ups 8 7", aliases={"pull ups": ["Pull-ups"]})

    assert parsed is not None
    assert parsed.exercise == "Pull-ups"
    assert [item.weight_kg for item in parsed.sets] == [None, None]
    assert parsed.load_mode == "bodyweight"


def test_existing_database_alias_points_to_its_unchanged_exercise_name(monkeypatch):
    def query(sql, _params=()):
        if "exercise_aliases" in sql:
            return [{"alias": "Bench Press", "exercise": "Bench Press"}]
        return []

    monkeypatch.setattr(db, "query", query)

    aliases = load_aliases()
    parsed = parse_set_message("Bench Press 80 8", aliases)

    assert aliases["bench press"] == ["Bench Press"]
    assert parsed.exercise == "Bench Press"
    assert parsed.known is True


def test_built_in_alias_is_ambiguous_when_multiple_existing_exercises_match(monkeypatch):
    def query(sql, _params=()):
        if "exercise_aliases" in sql:
            return []
        return [
            {"alias": "Bank", "exercise": "Bank"},
            {"alias": "Bench Press", "exercise": "Bench Press"},
        ]

    monkeypatch.setattr(db, "query", query)
    aliases = load_aliases()

    parsed = parse_set_message("BD 80 8", aliases)

    assert parsed is not None
    assert parsed.exercise is None
    assert set(parsed.ambiguous) == {"Bank", "Bench Press"}


def test_parser_and_alias_resolution_stay_under_300ms_p95():
    samples = []
    for _ in range(100):
        started = perf_counter()
        parse_set_message("Bankdrüken 80 kg 3x8")
        samples.append(perf_counter() - started)

    p95 = sorted(samples)[94]
    assert p95 < 0.3
