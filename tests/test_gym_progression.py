"""Double-Progression und PRs ohne Datenbank."""

from domains.gym.progression import (
    estimate_1rm, personal_records, recommend_next, progression_set_count,
)


def _sets(weight, reps):
    return [
        {"set_index": index, "weight_kg": weight, "reps": rep, "is_warmup": False}
        for index, rep in enumerate(reps, 1)
    ]


def test_first_session_uses_default_compound_target():
    suggestion = recommend_next(_sets(80, [8, 8, 7]), [], {"exercise_type": "compound"})

    assert suggestion == {"weight_kg": 80, "target_reps": [8, 8, 8], "incremented": False}


def test_set_count_can_come_from_default_plan_fallback():
    suggestion = recommend_next(
        _sets(80, [8]), [], {"exercise_type": "compound"}, set_count=3
    )

    assert suggestion["target_reps"] == [8, 8, 8]


def test_set_count_prefers_current_input_then_plan_then_default():
    assert progression_set_count(current_count=1, planned_count=4) == 4
    assert progression_set_count(current_count=0, planned_count=4) == 4
    assert progression_set_count(current_count=0, planned_count=None) == 3
    assert progression_set_count(current_count=1, previous_count=3, planned_count=None) == 3


def test_single_set_input_keeps_the_previous_session_target_count():
    current = _sets(80, [8])
    previous = _sets(80, [8, 8, 7])
    count = progression_set_count(
        current_count=len(current), previous_count=len(previous), planned_count=None
    )

    suggestion = recommend_next(
        current, previous, {"exercise_type": "compound"}, set_count=count
    )

    assert suggestion["target_reps"] == [8, 8, 7]
    assert len(suggestion["target_reps"]) == 3


def test_first_isolation_session_starts_at_ten_reps():
    suggestion = recommend_next(
        _sets(12, [10, 10]), [], {"exercise_type": "isolation"}
    )

    assert suggestion["target_reps"] == [10, 10]


def test_missed_rep_repeats_best_per_set_position():
    suggestion = recommend_next(
        _sets(80, [8, 8, 7]), _sets(80, [8, 8, 8]),
        {"exercise_type": "compound", "equipment": "barbell"},
    )

    assert suggestion["weight_kg"] == 80
    assert suggestion["target_reps"] == [8, 8, 8]
    assert suggestion["incremented"] is False


def test_meeting_previous_best_adds_one_rep_per_set():
    suggestion = recommend_next(
        _sets(80, [8, 8, 8]), _sets(80, [8, 8, 7]),
        {"exercise_type": "compound", "equipment": "barbell"},
    )

    assert suggestion["weight_kg"] == 80
    assert suggestion["target_reps"] == [9, 9, 9]


def test_upper_rep_boundary_increases_barbell_weight_and_resets_goal():
    suggestion = recommend_next(
        _sets(80, [10, 10, 10]), _sets(80, [10, 10, 10]),
        {"exercise_type": "compound", "equipment": "barbell"},
    )

    assert suggestion == {"weight_kg": 82.5, "target_reps": [6, 6, 6], "incremented": True}


def test_dumbbell_progression_increments_per_hantel():
    suggestion = recommend_next(
        _sets(22.5, [12, 12]), _sets(22.5, [12, 12]),
        {"exercise_type": "isolation", "equipment": "dumbbell"},
    )

    assert suggestion["weight_kg"] == 23.5
    assert suggestion["target_reps"] == [8, 8]


def test_machine_uses_configured_step_or_spec_fallback():
    configured = recommend_next(
        _sets(40, [12, 12]), _sets(40, [12, 12]),
        {"exercise_type": "isolation", "equipment": "machine", "weight_increment_kg": 5},
    )
    fallback = recommend_next(
        _sets(40, [12, 12]), _sets(40, [12, 12]),
        {"exercise_type": "isolation", "equipment": "machine"},
    )

    assert configured["weight_kg"] == 45
    assert fallback["weight_kg"] == 42.5


def test_unknown_equipment_does_not_invent_a_weight_jump():
    suggestion = recommend_next(
        _sets(40, [12, 12]), _sets(40, [12, 12]), {"exercise_type": "isolation"}
    )

    assert suggestion["weight_kg"] == 40
    assert suggestion["target_reps"] == [12, 12]
    assert suggestion["incremented"] is False


def test_warmups_do_not_change_progression_or_prs():
    warmup = {"set_index": 1, "weight_kg": 100, "reps": 10, "is_warmup": True}
    work = _sets(80, [8, 8])
    suggestion = recommend_next(work + [warmup], [], {"exercise_type": "compound"})
    records = personal_records(work, [warmup, *_sets(80, [7, 7])])

    assert suggestion["weight_kg"] == 80
    assert records["weight"] is False
    assert records["reps"] is True


def test_prs_compare_weight_reps_at_same_load_and_epley_estimate():
    old = _sets(80, [8, 8])
    current = _sets(82.5, [8, 7])

    records = personal_records(current, old)

    assert records == {"weight": True, "reps": True, "e1rm": True}
    assert estimate_1rm(80, 8) == 101.33333333333333


def test_pullup_added_load_never_gets_total_load_e1rm():
    records = personal_records(_sets(10, [8]), _sets(5, [8]), load_mode="added")

    assert records["weight"] is True
    assert records["e1rm"] is False


def test_bodyweight_sets_do_not_make_weight_or_e1rm_prs():
    records = personal_records(_sets(None, [10]), _sets(None, [9]), load_mode="bodyweight")

    assert records == {"weight": False, "reps": True, "e1rm": False}

