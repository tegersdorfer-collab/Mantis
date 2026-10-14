"""Enge Gym-Intent-Erkennung und Bestätigungsschritte."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from domains.gym import coach as coach_module
from domains.gym.aliases import builtin_aliases
from domains.gym.coach import GymCoach


def _aliases():
    return builtin_aliases()


def test_clear_set_line_is_a_deterministic_log_intent(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)

    intent = GymCoach().classify("Bank 80 8 8 7")

    assert intent is not None
    assert intent.action == "set_logged"
    assert intent.parsed.exercise == "Bankdrücken"


def test_generic_curls_ask_for_equipment_instead_of_being_ignored(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    coach = GymCoach()

    intent = coach.classify("Curls 15 4x10")
    assert intent is not None
    assert intent.action == "clarify_equipment"

    result = asyncio.run(coach.process(intent))

    assert "Kurzhantel" in result.text
    assert "SZ" in result.text
    assert "Kabel" in result.text


def test_bare_today_question_stays_with_the_normal_agent(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    assert GymCoach().classify("Was mach ich heute?") is None


def test_today_question_is_gym_only_with_an_open_session(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(
        coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7}
    )

    intent = GymCoach().classify("Was mach ich heute?")

    assert intent is not None
    assert intent.action == "next_workout"


def test_training_smalltalk_is_not_a_gym_action(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    assert GymCoach().classify("Training war super, danke!") is None


def test_history_and_finish_have_separate_actions(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})

    coach = GymCoach()

    assert coach.classify("Bank Verlauf").action == "history"
    assert coach.classify("Training fertig").action == "finish"


def test_unknown_exercise_requires_an_open_session_and_explicit_kg(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)
    coach = GymCoach()

    intent = coach.classify("Neue Übung 80 kg, 8 Wiederholungen, 8 Wiederholungen")

    assert intent is None
    assert coach.classify("80 kg wie viel sind das in pounds?") is None

    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})
    intent = coach.classify("Neue Übung 80 kg, 8 Wiederholungen, 8 Wiederholungen")

    assert intent is not None
    assert intent.action == "parser_fallback"


@pytest.mark.parametrize("text", ["Bank 8,8,7", "Bank 80 12,5"])
def test_ambiguous_or_decimal_reps_do_not_become_gym_actions(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    assert GymCoach().classify(text) is None


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
        "8 8 8",
        "80 8 8",
        "Bank 80",
        "Bank 0 8 8",
        "Bank 80 0 8",
        "Bank 80 31 8",
        "Bank 80kg 3x0",
        "Bank 80kg 0x8",
        "Bank 80kg 3 mal",
        "Laufband 80 8 8",
        "Kalorien 500 8 8",
    ],
)
def test_non_training_examples_do_not_enter_the_gym_path(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)
    coach = GymCoach()

    assert coach.classify(text) is None


def test_pending_unknown_exercise_is_saved_only_after_yes(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    saved = []

    def add_sets(parsed, *, allow_create, now):
        saved.append((parsed, allow_create))
        return {
            "exercise": parsed.exercise, "sets": [{"weight_kg": 42, "reps": 10}],
            "previous_sets": [], "progression": {"weight_kg": 42, "target_reps": [10]},
            "records": {},
        }

    monkeypatch.setattr(coach_module.session, "add_sets", add_sets)
    coach = GymCoach()
    parsed = coach_module.parse_set_message("Neue Übung 42 10 9")
    coach._latest_pending = lambda: {
        "kind": "new_exercise", "candidate": coach_module._parsed_payload(parsed),
        "expires_at": "2026-10-14T10:10:00+02:00",
    }
    confirmation = coach.classify("ja", now=datetime(2026, 10, 14, 8, tzinfo=timezone.utc))
    fallback = AsyncMock()

    result = asyncio.run(coach.process(confirmation, llm_fallback=fallback,
                                      now=datetime(2026, 10, 14, 8, tzinfo=timezone.utc)))

    assert result.action == "set_logged"
    assert "Neue Übung" in result.text
    assert saved[0][1] is True
    fallback.assert_not_awaited()


def test_pending_candidate_expired_after_ten_minutes(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", lambda: {})
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "new_exercise", "candidate": {},
        "expires_at": "2026-10-14T10:00:00+02:00",
    }

    intent = coach.classify("ja", now=datetime(2026, 10, 14, 10, 11, tzinfo=timezone.utc))

    assert intent is not None
    assert intent.action == "pending_expired"


def test_ambiguous_choice_expires_after_ten_minutes(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "ambiguous",
        "candidate": {"ambiguous": ["Bankdrücken", "Schulterdrücken"]},
        "expires_at": "2026-10-14T10:00:00+02:00",
    }

    intent = coach.classify(
        "Bankdrücken", now=datetime(2026, 10, 14, 10, 11, tzinfo=timezone.utc)
    )

    assert intent is not None
    assert intent.action == "pending_expired"


def test_resolved_pending_payload_is_not_reused(monkeypatch):
    monkeypatch.setattr(coach_module.db, "query", lambda *_args, **_kwargs: [
        {"meta": {"gym_pending": {"resolved": True}}},
    ])

    assert GymCoach()._latest_pending() is None


def test_old_pending_is_ignored_when_a_later_assistant_message_is_normal(monkeypatch):
    def query(sql):
        if "meta ? 'gym_pending'" in sql:
            return [{"meta": {"gym_pending": {"kind": "new_exercise"}}}]
        return [{"meta": {"use_case": "chat"}}]

    monkeypatch.setattr(coach_module.db, "query", query)

    coach = GymCoach()
    assert coach._latest_pending() is None
    assert coach.classify("ja") is None


def test_last_set_repair_keeps_the_claimed_old_value(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})

    intent = GymCoach().classify("letzter Satz 7 nicht 8")

    assert intent is not None
    assert intent.action == "correct_reps"
    assert intent.payload == {"reps": 7, "expected_reps": 8}


@pytest.mark.parametrize(
    "text",
    [
        "Lösch den letzten Satz aus dem Anschreiben",
        "Schreib als Beispiel: falsch, letzter Satz 7 nicht 8",
    ],
)
def test_correction_grammar_does_not_capture_text_edit_requests(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})

    assert GymCoach().classify(text) is None


def test_corrections_are_not_gym_actions_without_an_open_session(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    assert GymCoach().classify("letzter Satz 7 nicht 8") is None
    assert GymCoach().classify("Bank war 82,5") is None


@pytest.mark.parametrize(
    "text",
    ["Rechnung 80 8 8", "Bus 52 5 5"],
)
def test_three_plausible_numbers_alone_do_not_create_unknown_exercises(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    assert GymCoach().classify(text) is None


def test_unknown_exercise_without_kg_or_equipment_is_not_captured_even_in_open_session(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})

    assert GymCoach().classify("Miete 650 5") is None


def test_time_for_a_known_exercise_alias_is_not_logged_as_a_set(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: {"id": 7})

    intent = GymCoach().classify("Bank 14:30")

    assert intent is None or intent.action != "set_logged"


def test_known_exercise_with_unexplained_trailing_text_is_not_directly_logged(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    intent = GymCoach().classify("Bank 80 kg 3 Sätze à 8 morgen")

    assert intent is None or intent.action != "set_logged"


def test_known_set_shape_with_trailing_text_gets_confirmation_even_at_small_weights(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_args, **_kwargs: None)

    intent = GymCoach().classify("Bank 20 kg 8 8 morgen")

    assert intent is not None
    assert intent.action == "parser_fallback"


def test_pending_selection_accepts_a_custom_equipment_variant_name(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", lambda: {
        **_aliases(), "rudern kh": ["Rudern KH"], "rudern lh": ["Rudern LH"],
    })
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "ambiguous",
        "candidate": {"ambiguous": ["Rudern KH", "Rudern LH"]},
        "expires_at": "2026-10-14T10:10:00+02:00",
    }

    intent = coach.classify("Rudern KH", now=datetime(2026, 10, 14, 8, tzinfo=timezone.utc))

    assert intent is not None
    assert intent.action == "pending_selected"
    assert intent.payload["selected"] == "Rudern KH"


def test_equipment_free_builtin_alias_asks_when_database_has_multiple_variants(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", lambda: {
        **_aliases(), "schulterdruecken": ["KH Schulterdrücken", "LH Schulterdrücken"],
    })

    intent = GymCoach().classify("Schulterdrücken 22,5 10")

    assert intent is not None
    assert intent.action == "ambiguous_exercise"
    assert set(intent.parsed.ambiguous) == {"KH Schulterdrücken", "LH Schulterdrücken"}


def test_last_set_repair_reports_a_mismatch_without_claiming_success(monkeypatch):
    calls = []

    def correct(reps, now=None, *, expected_reps=None):
        calls.append((reps, expected_reps))
        return {"conflict": True, "reps": 9}

    monkeypatch.setattr(coach_module.session, "correct_last_reps", correct)
    intent = coach_module.GymIntent(
        "correct_reps", payload={"reps": 7, "expected_reps": 8}
    )

    result = asyncio.run(GymCoach().process(intent))

    assert calls == [(7, 8)]
    assert "9 Wdh." in result.text
    assert "nicht 8" in result.text
    assert "Korrigiert" not in result.text


def test_split_pending_does_not_capture_unrelated_chat(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "split", "expires_at": "2026-10-14T10:10:00+02:00",
    }

    assert coach.classify("Guten Morgen") is None


def test_llm_fallback_preserves_ambiguous_alias_for_choice(monkeypatch):
    aliases = {**_aliases(), "press": ["Bankdrücken", "Schulterdrücken"]}
    monkeypatch.setattr(coach_module, "load_aliases", lambda: aliases)
    coach = GymCoach()
    fallback = AsyncMock(return_value=(
        '{"exercise":"Press","sets":[{"weight_kg":40,"reps":8}]}', []
    ))
    intent = coach_module.GymIntent("parser_fallback", payload={"text": "Press 40 bitte loggen"})

    result = asyncio.run(coach.process(intent, llm_fallback=fallback))

    assert result.pending["kind"] == "ambiguous"
    assert "Bankdrücken oder Schulterdrücken" in result.text


@pytest.mark.parametrize(
    "candidate",
    [
        {"exercise": "Bankdrücken", "sets": [{"weight_kg": float("nan"), "reps": 8}]},
        {"exercise": "Bankdrücken", "sets": [{"weight_kg": 10001, "reps": 8}]},
        {"exercise": "Bankdrücken", "sets": [{"weight_kg": 80, "reps": 8.5}]},
    ],
)
def test_llm_fallback_rejects_invalid_numeric_candidates(candidate):
    assert GymCoach._candidate_from_model(candidate, _aliases()) is None


def test_split_answer_is_saved_and_suggests_first_unit(monkeypatch):
    saved = []
    monkeypatch.setattr(coach_module.db, "set_setting", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(coach_module.session, "workout_exercise_history", lambda **_kwargs: [])
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "split", "expires_at": "2026-10-14T10:10:00+02:00",
    }
    moment = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
    intent = coach.classify("Push Pull Beine", now=moment)

    result = asyncio.run(coach.process(intent, now=moment))

    assert saved == [("gym_split", "Push/Pull/Beine")]
    assert result.action == "split_answer"
    assert "nächste einheit: push" in result.text.lower()


def test_history_reply_shows_last_sessions_and_epley_trend(monkeypatch):
    monkeypatch.setattr(coach_module.session, "exercise_history", lambda *_args, **_kwargs: [
        {"workout_id": 2, "date": "2026-10-14", "created_at": "2026-10-14T10:00:00+02:00",
         "sets": [{"weight_kg": 80, "reps": 8}]},
        {"workout_id": 1, "date": "2026-10-01", "created_at": "2026-10-01T10:00:00+02:00",
         "sets": [{"weight_kg": 75, "reps": 8}]},
    ])

    result = GymCoach()._history("Bankdrücken")

    assert result.action == "history"
    assert "14.10." in result.text and "01.10." in result.text
    assert "e1RM +" in result.text
    assert "Bestwert" in result.text


def test_finish_reply_summarizes_workout_and_prs(monkeypatch):
    monkeypatch.setattr(coach_module.session, "finish", lambda *_args: {
        "workout_id": 5, "timed_out": False, "duration_min": 47,
        "exercise_count": 2, "set_count": 6, "volume_kg": 1730.0,
        "exercises": ["Bankdrücken", "Kniebeuge"], "prs": ["Bankdrücken"],
    })

    result = asyncio.run(GymCoach().process(coach_module.GymIntent("finish")))

    assert "47 Min" in result.text
    assert "6 Sätze" in result.text
    assert "1730 kg Volumen" in result.text
    assert "🏆 Bankdrücken" in result.text


def test_dumbbell_reply_uses_german_decimal_and_per_hand_weight():
    result = GymCoach._log_reply({
        "exercise": "Schulterdrücken",
        "sets": [
            {"weight_kg": 22.5, "reps": 10},
            {"weight_kg": 22.5, "reps": 9},
        ],
        "previous_sets": [],
        "progression": {"weight_kg": 23.5, "target_reps": [10, 10]},
        "metadata": {"equipment": "dumbbell", "dumbbell_pair": True},
        "records": {},
    })

    assert "22,5 kg je Hantel" in result
    assert "nächstes Mal: 23,5 kg je Hantel" in result


def test_next_workout_uses_a_matching_active_plan_before_asking_split(monkeypatch):
    monkeypatch.setattr(coach_module.db, "get_setting", lambda _key: None)
    monkeypatch.setattr(coach_module.session, "workout_exercise_history", lambda **_kwargs: [])
    monkeypatch.setattr(coach_module.fitness, "active_plan", lambda: {
        "plan_json": {
            "upperA": [{"name": "Bench Press", "weight": 80, "reps": 8, "sets": 4}],
            "lowerA": [{"name": "Squat", "weight": 100, "reps": 5, "sets": 4}],
        },
    })

    result = GymCoach()._next_workout(datetime(2026, 10, 14, 10, tzinfo=timezone.utc))

    assert result.action == "next_workout"
    assert "Upper A" in result.text
    assert "Bench Press 80 kg, Ziel 8" in result.text
    assert result.pending is None


def test_upper_lower_rotation_classifies_workouts_from_exercise_categories(monkeypatch):
    monkeypatch.setattr(coach_module.db, "get_setting", lambda _key: "Upper/Lower")
    monkeypatch.setattr(coach_module.fitness, "active_plan", lambda: None)
    monkeypatch.setattr(coach_module.session, "workout_exercise_history", lambda **_kwargs: [
        {"workout_id": 9, "date": "2026-10-14", "created_at": "2026-10-14T10:00:00+02:00",
         "exercise": "Bankdrücken", "muscle": "chest", "reps": 8, "weight_kg": 80},
        {"workout_id": 9, "date": "2026-10-14", "created_at": "2026-10-14T10:00:00+02:00",
         "exercise": "Klimmzüge", "muscle": "back", "reps": 8, "weight_kg": None},
    ])

    result = GymCoach()._next_workout(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))

    assert "Nächste Einheit: Lower" in result.text


def test_history_trend_uses_creation_time_for_workouts_on_the_same_date(monkeypatch):
    monkeypatch.setattr(coach_module.session.db, "query", lambda *_args, **_kwargs: [
        {"workout_id": 2, "date": "2026-10-14", "created_at": "2026-10-14T11:00:00+02:00",
         "set_index": 1, "reps": 8, "weight_kg": 85, "is_warmup": False,
         "exercise": "Bank", "load_mode": "external"},
        {"workout_id": 1, "date": "2026-10-14", "created_at": "2026-10-14T10:00:00+02:00",
         "set_index": 1, "reps": 8, "weight_kg": 80, "is_warmup": False,
         "exercise": "Bank", "load_mode": "external"},
    ])

    result = GymCoach()._history("Bank")

    assert "e1RM +6.3 kg" in result.text


@pytest.mark.parametrize("text", [
    "letzter Satz -7 nicht 8", "falsch, letzter Satz 7 nicht -8", "Bank war -82,5",
])
def test_negative_numbers_are_never_a_correction(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    monkeypatch.setattr(coach_module.session, "open_session", lambda *_a, **_k: {"id": 1})

    assert GymCoach().classify(text) is None


def test_leg_curls_do_not_turn_a_lower_session_into_full_body():
    rows = [{"exercise": "Beincurls", "muscle": "legs"}]

    assert GymCoach._workout_category(rows, ["Lower", "Upper"]) == "Lower"


def test_leg_curls_without_muscle_metadata_still_count_as_lower():
    rows = [{"exercise": "Seated Leg Curls", "muscle": None}]

    assert GymCoach._workout_category(rows, ["Lower", "Upper"]) == "Lower"


def test_lower_upper_rotation_after_leg_curls_suggests_upper(monkeypatch):
    monkeypatch.setattr(coach_module.db, "get_setting", lambda _key: "Lower/Upper")
    monkeypatch.setattr(coach_module.fitness, "active_plan", lambda: None)
    monkeypatch.setattr(coach_module.session, "workout_exercise_history", lambda **_kwargs: [
        {"workout_id": 9, "date": "2026-10-14", "created_at": "2026-10-14T10:00:00+02:00",
         "exercise": "Beincurls", "muscle": "legs", "reps": 10, "weight_kg": 40},
    ])

    result = GymCoach()._next_workout(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))

    assert "Nächste Einheit: Upper" in result.text


@pytest.mark.parametrize("text", [
    "Bitte push den Commit", "Push und dann Feierabend", "Beine hoch",
    "Upper Lower bitte", "Das war ein Ganzkörper Tag oder",
])
def test_open_split_question_ignores_messages_that_are_not_only_split_names(monkeypatch, text):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "split", "expires_at": "2099-10-14T10:10:00+02:00",
    }

    intent = coach.classify(text, now=datetime(2026, 10, 14, 8, tzinfo=timezone.utc))

    assert intent is None


@pytest.mark.parametrize(("text", "expected"), [
    ("Push/Pull/Beine", ["Push", "Pull", "Beine"]),
    ("PPL", ["Push", "Pull", "Beine"]),
    ("Upper/Lower", ["Upper", "Lower"]),
    ("Ganzkörper", ["Ganzkörper"]),
    ("Push, Pull, Legs", ["Push", "Pull", "Beine"]),
    ("Bitte push den Commit", []),
    ("Push Pull und Beine", []),
])
def test_split_names_must_make_up_the_whole_message(text, expected):
    assert GymCoach._split_sequence(text) == expected


def test_complete_split_name_answers_the_open_question(monkeypatch):
    monkeypatch.setattr(coach_module, "load_aliases", _aliases)
    coach = GymCoach()
    coach._latest_pending = lambda: {
        "kind": "split", "expires_at": "2099-10-14T10:10:00+02:00",
    }

    intent = coach.classify("Push/Pull/Beine", now=datetime(2026, 10, 14, 8, tzinfo=timezone.utc))

    assert intent is not None and intent.action == "split_answer"
