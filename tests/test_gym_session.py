"""Session-Zeitgrenzen und Abschlusszusammenfassung."""

from datetime import datetime, timedelta, timezone

from domains.gym.session import _exercise_defaults, session_timeout_end, summarize_session


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

