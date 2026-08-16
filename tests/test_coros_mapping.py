"""Tests für den COROS-Prosa-Parser (domains/coros/mapping.py).

Reine Funktionen, keine DB, kein Netz. Die Fixtures unter tests/fixtures/coros/
haben das echte Antwortformat, aber erfundene Zahlen — die echten Dumps liegen
gitignored unter data/coros_samples/ und enthalten personenbezogene Daten.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import mapping

FIX = Path(__file__).parent / "fixtures" / "coros"


def _fix(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── check_response ──────────────────────────────────────────────────────────

def test_server_error_text_raises_even_without_iserror():
    with pytest.raises(mapping.CorosFormatError, match="anomalies"):
        mapping.check_response(_fix("error_anomalies.txt"))


def test_date_format_complaint_raises():
    with pytest.raises(mapping.CorosFormatError):
        mapping.check_response("startDate must be in yyyyMMdd format.")


def test_empty_response_raises():
    with pytest.raises(mapping.CorosFormatError):
        mapping.check_response("   ")


def test_coros_api_error_text_raises():
    with pytest.raises(mapping.CorosFormatError):
        mapping.check_response("COROS API error: The date is out of range")


def test_normal_text_passes_through_unchanged():
    text = _fix("daily_health.txt")
    assert mapping.check_response(text) == text


# ── parse_int ───────────────────────────────────────────────────────────────

def test_parse_int_strips_thousands_separator():
    assert mapping.parse_int("12,500") == 12500


def test_parse_int_plain():
    assert mapping.parse_int("692") == 692


def test_parse_int_no_data_is_none():
    assert mapping.parse_int("No data") is None


def test_parse_int_garbage_is_none():
    assert mapping.parse_int("") is None
    assert mapping.parse_int("--") is None


def test_parse_int_well_formed_multi_group_thousands():
    assert mapping.parse_int("1,048") == 1048


def test_parse_int_truncated_thousands_group_is_none():
    # '1,048 kcal' mitten im Stream abgeschnitten zu '1,0' — darf weder als 10
    # (alte Regex: Komma weggeworfen) noch als 1 (führendes Fragment) durchgehen.
    assert mapping.parse_int("1,0") is None


# ── parse_duration_h ────────────────────────────────────────────────────────

def test_duration_hours_and_minutes():
    assert mapping.parse_duration_h("8h 10min") == 8.17


def test_duration_minutes_only():
    assert mapping.parse_duration_h("59 min") == 0.98


def test_duration_zero():
    assert mapping.parse_duration_h("0 min") == 0.0


def test_duration_hours_with_zero_minutes():
    assert mapping.parse_duration_h("2h 0min") == 2.0


def test_duration_no_data_is_none():
    assert mapping.parse_duration_h("No data") is None


# ── sane ────────────────────────────────────────────────────────────────────

def test_sane_passes_plausible_value():
    assert mapping.sane("steps", 9677) == 9677


def test_sane_rejects_impossible_value():
    # Ein Parser-Ausrutscher, der zwei Zahlen verklebt, darf nicht in die DB.
    assert mapping.sane("steps", 46201138) is None


def test_sane_rejects_negative():
    assert mapping.sane("resting_hr", -5) is None


def test_sane_passes_none_through():
    assert mapping.sane("steps", None) is None


def test_sane_unknown_column_passes_through():
    assert mapping.sane("gibt_es_nicht", 42) == 42


# ── parse_daily_health ──────────────────────────────────────────────────────

def test_daily_health_finds_every_day():
    days = mapping.parse_daily_health(_fix("daily_health.txt"))
    assert sorted(days) == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]


def test_daily_health_basic_columns():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-11"]
    assert d["steps"] == 12500
    assert d["active_calories"] == 1200
    assert d["exercise_minutes"] == 25
    assert d["stress_avg"] == 50


def test_daily_health_sleep_stages():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-10"]
    assert d["sleep_duration"] == 7.5
    assert d["sleep_deep"] == 1.5
    assert d["sleep_core"] == 4.0   # "Light" ist Apples "Core"
    assert d["sleep_rem"] == 1.75
    assert d["sleep_awake"] == 0.25


def test_daily_health_day_without_sleep_block_keeps_other_fields():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-12"]
    assert d["steps"] == 8000
    assert "sleep_duration" not in d


def test_daily_health_no_data_field_is_omitted_not_zero():
    # "Stress: No data" darf nicht als 0 in der DB landen — 0 wäre eine Aussage.
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-12"]
    assert "stress_avg" not in d


def test_daily_health_does_not_map_sleep_hr_to_daily_hr():
    # "Sleep HR" ist Schlaf-Puls, nicht Tages-Puls. hr_avg kommt aus queryAvgHeartRate.
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-10"]
    assert "hr_avg" not in d and "hr_min" not in d and "hr_max" not in d


def test_daily_health_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_daily_health(_fix("error_anomalies.txt"))


def test_daily_health_unrecognisable_text_yields_nothing():
    # Kein Tagesmarker → leeres Ergebnis, keine Ausnahme: der Tag fehlt eben.
    assert mapping.parse_daily_health("Daily Health Data\n====\n\nNichts hier.") == {}


def test_daily_health_truncated_stream_yields_no_steps_value():
    # Ein SSE-Stream, der mitten in 'Steps: 1,048' abbricht, darf keinen falschen
    # steps-Wert erzeugen — der ganze Tag fällt dann eben ganz raus.
    days = mapping.parse_daily_health("--- 20260810 ---\nSteps: 1,0")
    assert "2026-08-10" not in days


def test_daily_health_repeated_day_marker_merges_not_overwrites():
    # Zwei Blöcke für denselben Tag (z.B. bei einem gefensterten Re-Query) müssen
    # sich zusammenfügen, wie parse_training_load es mit setdefault schon tut —
    # nicht den ersten Block verwerfen.
    text = (
        "--- 20260810 ---\n"
        "Steps: 5,000 | Calories: 400 kcal | Exercise: 5 min\n"
        "--- 20260810 ---\n"
        "Stress: Avg 20\n"
    )
    d = mapping.parse_daily_health(text)["2026-08-10"]
    assert d["steps"] == 5000
    assert d["stress_avg"] == 20


# ── parse_sleep ─────────────────────────────────────────────────────────────

def test_sleep_reads_score_per_day():
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert days["2026-08-10"]["sleep_score"] == 77
    assert days["2026-08-11"]["sleep_score"] == 38


def test_sleep_no_data_score_is_omitted():
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert "2026-08-12" not in days


def test_sleep_does_not_emit_stage_columns():
    # Die Anteile sind Prozente; die absoluten Stunden kommen aus parse_daily_health.
    day = mapping.parse_sleep(_fix("sleep_data.txt"))["2026-08-10"]
    assert set(day) == {"sleep_score"}


def test_sleep_ignores_corrupt_1982_nap_window():
    # COROS liefert vereinzelt Zeitstempel von 1982 (echter Datenfehler im Dump).
    # Geschützt wird das nicht durch eine Jahresgrenze, sondern dadurch, dass nur
    # eine Zeile, die ausschliesslich aus einem Datum besteht, als Tagesmarker zählt.
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert not any(d.startswith("1982") for d in days)


def test_sleep_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep(_fix("error_anomalies.txt"))


# ── parse_sleep_hrv ─────────────────────────────────────────────────────────

def test_hrv_reads_daily_average():
    days = mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))
    assert days["2026-08-12"]["hrv"] == 71
    assert days["2026-08-11"]["hrv"] == 39


def test_hrv_no_data_day_is_omitted():
    assert "2026-08-10" not in mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))


def test_hrv_ignores_raw_series_section():
    # Die Rohzeitreihe enthält 'hrv=61 ms' — daraus darf kein Tag entstehen.
    days = mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))
    assert sorted(days) == ["2026-08-11", "2026-08-12"]


def test_hrv_does_not_read_baseline_as_value():
    # 'Baseline: 48 ms' steht direkt unter dem Tageswert und darf ihn nicht überschreiben.
    assert mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))["2026-08-12"]["hrv"] == 71


def test_hrv_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep_hrv(_fix("error_anomalies.txt"))


# ── parse_resting_hr / parse_avg_hr ─────────────────────────────────────────

def test_resting_hr_per_day():
    days = mapping.parse_resting_hr(_fix("resting_hr.txt"))
    assert days["2026-08-11"]["resting_hr"] == 47
    assert days["2026-08-10"]["resting_hr"] == 46


def test_resting_hr_no_data_day_omitted():
    assert "2026-08-12" not in mapping.parse_resting_hr(_fix("resting_hr.txt"))


def test_resting_hr_ignores_stale_value_mentioned_in_no_data_line():
    # Das Modul geht davon aus, dass COROS' Text nicht formatstabil ist — 'der Rest
    # der Zeile ist immer NN bpm oder No data' ist unsicher. Ein alter, in Klammern
    # genannter Wert darf nicht als frische Messung übernommen werden.
    days = mapping.parse_resting_hr("2026-08-10: No data (last known 58 bpm)")
    assert "2026-08-10" not in days


def test_avg_hr_reads_avg_min_max():
    day = mapping.parse_avg_hr(_fix("avg_hr.txt"))["2026-08-11"]
    assert day == {"hr_avg": 85, "hr_min": 52, "hr_max": 140}


def test_avg_hr_no_data_day_omitted():
    assert "2026-08-10" not in mapping.parse_avg_hr(_fix("avg_hr.txt"))


def test_avg_hr_ignores_stale_value_mentioned_in_no_data_line():
    # Dieselbe Gefahr wie bei parse_resting_hr, nur ohne die schützende Klammer:
    # ohne Anker griffe sich parse_int die erste Zahl im Freitext.
    days = mapping.parse_avg_hr("2026-08-11: No data, last known 58 bpm")
    assert "2026-08-11" not in days


# ── parse_training_load ─────────────────────────────────────────────────────

def test_training_load_per_day():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-12"]
    assert day == {"training_load_short": 12, "training_load_long": 60}


def test_training_load_ignores_ratio_and_comment():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-11"]
    assert set(day) == {"training_load_short", "training_load_long"}


# ── tageslose Parser ────────────────────────────────────────────────────────

def test_fitness_overview_reads_vo2max_only():
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt")) == {"vo2max": 47}


def test_fitness_overview_does_not_confuse_running_level_with_vo2max():
    # 'Running Level: 70' steht direkt darunter und ist keine VO2max.
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt"))["vo2max"] == 47


def test_recovery_reads_percentage():
    assert mapping.parse_recovery(_fix("recovery.txt")) == {"recovery_pct": 82}


def test_flat_parsers_return_empty_dict_when_field_absent():
    assert mapping.parse_recovery("Recovery Status\n====\n\nLevel: unbekannt") == {}


def test_flat_parsers_reject_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_recovery(_fix("error_anomalies.txt"))
