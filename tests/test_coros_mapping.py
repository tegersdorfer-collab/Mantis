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


def test_parse_int_truncated_two_digit_group_is_none():
    assert mapping.parse_int("1,04") is None


def test_parse_int_plain_four_digit_without_separator():
    # Wohlgeformte Zahl ohne Tausendertrennzeichen — kein abgebrochener Stream,
    # darf also nicht an der Dreiergruppen-Regel scheitern.
    assert mapping.parse_int("12500") == 12500


def test_parse_int_negative():
    assert mapping.parse_int("-5") == -5


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


def test_daily_health_unrecognisable_text_raises_when_no_day_markers():
    # Kein einziger Tagesmarker → das ist keine leere Antwort, sondern ein
    # unerkanntes Format (z.B. ein durchgerutschter Fehlertext ohne Marker-Wort).
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_daily_health("Daily Health Data\n====\n\nNichts hier.")


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


def test_sleep_derives_stage_hours_from_ratios():
    # Die Phasen stehen hier als Anteil der Hauptschlafzeit, nicht als Dauer.
    # parse_daily_health liefert die exakteren Dauern, aber nur ~14 Tage zurück —
    # deshalb rechnen wir sie hier aus, damit ältere Tage nicht leer bleiben.
    day = mapping.parse_sleep(_fix("sleep_data.txt"))["2026-08-10"]
    assert day["sleep_duration"] == 7.08          # 7h 5min
    assert day["sleep_deep"] == 1.56              # 22 % davon
    assert day["sleep_core"] == 4.11              # 58 % (COROS nennt es "Light")
    assert day["sleep_rem"] == 1.13               # 16 %


def test_sleep_awake_time_is_read_directly_not_from_ratio():
    # "Awake Time" steht als Dauer da — der Anteil wäre die ungenauere Quelle.
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert days["2026-08-10"]["sleep_awake"] == 0.3      # 18 min
    assert days["2026-08-11"]["sleep_awake"] == 1.08     # 1h 5min


def test_sleep_reads_bedtime_window():
    day = mapping.parse_sleep(_fix("sleep_data.txt"))["2026-08-10"]
    assert day["sleep_start"] == "22:35"
    assert day["sleep_end"] == "05:58"


def test_sleep_reads_awake_count_and_naps():
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert days["2026-08-10"]["sleep_awake_count"] == 2
    assert days["2026-08-10"]["nap_duration"] == 0.42     # 25 min
    # 0 min Nickerchen ist eine echte Aussage, kein fehlender Wert.
    assert days["2026-08-11"]["nap_duration"] == 0.0


def test_sleep_ignores_corrupt_1982_nap_window():
    # COROS liefert vereinzelt Zeitstempel von 1982 (echter Datenfehler im Dump).
    # Geschützt wird das nicht durch eine Jahresgrenze, sondern dadurch, dass nur
    # eine Zeile, die ausschliesslich aus einem Datum besteht, als Tagesmarker zählt.
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert not any(d.startswith("1982") for d in days)


def test_sleep_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep(_fix("error_anomalies.txt"))


def test_sleep_unrecognisable_text_raises_when_no_day_markers():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep("Sleep Data\n====\n\nNichts hier.")


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


def test_hrv_unrecognisable_text_raises_when_no_day_markers():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep_hrv("Sleep HRV\n====\n\nNichts hier.")


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


def test_resting_hr_unrecognisable_text_raises_when_no_day_markers():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_resting_hr("Resting Heart Rate\n====\n\nNichts hier.")


def test_resting_hr_all_no_data_returns_empty_without_raising():
    # Die Falle: _RHR_LINE verlangt 'NN bpm' und matcht 'No data' nicht — die
    # Strukturprüfung darf sich darum nicht auf dieses Muster stützen, sonst
    # würde eine Woche voller 'No data'-Tage fälschlich als Formatbruch gelten.
    days = mapping.parse_resting_hr("2026-08-12: No data\n2026-08-11: No data")
    assert days == {}


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


def test_avg_hr_unrecognisable_text_raises_when_no_day_markers():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_avg_hr("Average Heart Rate\n====\n\nNichts hier.")


def test_avg_hr_all_no_data_returns_empty_without_raising():
    # Dieselbe Falle wie bei parse_resting_hr: _AVG_HR_LINE verlangt 'NN bpm'.
    days = mapping.parse_avg_hr("2026-08-12: No data\n2026-08-11: No data")
    assert days == {}


# ── parse_training_load ─────────────────────────────────────────────────────

def test_training_load_per_day():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-12"]
    assert day == {
        "training_load_short": 12, "training_load_long": 60,
        "training_load_ratio": 0.2, "training_load_trend": "Decreasing",
    }


def test_training_load_unrecognisable_text_raises_when_no_day_markers():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_training_load("Training Load Assessment\n====\n\nNichts hier.")


# ── tageslose Parser ────────────────────────────────────────────────────────

def test_fitness_overview_reads_vo2max():
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt"))["vo2max"] == 47


def test_fitness_overview_does_not_confuse_running_level_with_vo2max():
    # 'Running Level: 70' steht direkt darunter und ist keine VO2max.
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt"))["vo2max"] == 47


def test_recovery_reads_percentage():
    assert mapping.parse_recovery(_fix("recovery.txt"))["recovery_pct"] == 82


def test_recovery_no_data_returns_empty_without_raising():
    # Das Label war da, nur der Wert fehlt — das ist Fall 1 (legitim), kein
    # Formatbruch. Muss von "Label gar nicht vorhanden" unterscheidbar bleiben.
    assert mapping.parse_recovery("Recovery Status\n====\n\nRecovery: No data") == {}


def test_flat_parsers_raise_when_label_absent():
    # Fehlt das Label ganz, ist das kein leeres Ergebnis mehr, sondern ein
    # unerkanntes Format — der Aufrufer soll das nicht als "keine Daten" lesen.
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_recovery("Recovery Status\n====\n\nLevel: unbekannt")


def test_flat_parsers_reject_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_recovery(_fix("error_anomalies.txt"))


# ── neue Felder: Puls im Schlaf ─────────────────────────────────────────────

def test_daily_health_reads_sleep_heart_rate():
    day = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-10"]
    assert day["sleep_hr_avg"] == 55
    assert day["sleep_hr_min"] == 45
    assert day["sleep_hr_max"] == 70


def test_daily_health_day_without_sleep_summary_has_no_sleep_hr():
    day = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-12"]
    assert "sleep_hr_avg" not in day


# ── neue Felder: Trainingslast ──────────────────────────────────────────────

def test_training_load_reads_ratio_and_trend():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-12"]
    assert day["training_load_ratio"] == 0.2
    assert day["training_load_trend"] == "Decreasing"


# ── neue Felder: Fitness-Assessment ─────────────────────────────────────────

def test_fitness_overview_reads_level_and_pace_in_seconds():
    f = mapping.parse_fitness_overview(_fix("fitness_overview.txt"))
    assert f["running_level"] == 70
    assert f["threshold_pace_sec"] == 300          # 5:00 /km


def test_fitness_overview_reads_race_predictions_in_seconds():
    f = mapping.parse_fitness_overview(_fix("fitness_overview.txt"))
    assert f["race_5k_sec"] == 1440                # 24:00
    assert f["race_10k_sec"] == 3000               # 50:00
    assert f["race_half_sec"] == 6600              # 1:50:00
    assert f["race_marathon_sec"] == 13800         # 3:50:00


# ── neue Felder: Erholung ───────────────────────────────────────────────────

def test_recovery_reads_level_text_and_remaining_hours():
    r = mapping.parse_recovery(_fix("recovery.txt"))
    assert r["recovery_level"] == "Moderate training allowed"
    assert r["recovery_full_h"] == 4.0


# ── neue Quelle: Profil ─────────────────────────────────────────────────────

def test_user_info_reads_weight():
    assert mapping.parse_user_info(_fix("user_info.txt"))["weight"] == 61.4


def test_user_info_reads_profile_fields():
    p = mapping.parse_user_info(_fix("user_info.txt"))
    assert p["height_cm"] == 168
    assert p["birthday"] == "1994-11-02"
    assert p["gender"] == "Female"
    assert p["nickname"] == "Testkonto"


def test_user_info_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_user_info(_fix("error_anomalies.txt"))


# ── neue Quelle: Kopfzeile der Tagesantwort ─────────────────────────────────

def test_daily_header_reads_hrv_baseline():
    # Steht in der Kopfzeile, gilt also für den ganzen Abruf — ein Momentanwert,
    # kein Tageswert. Landet deshalb in health_assessment, nicht in health_data.
    h = mapping.parse_daily_header(_fix("daily_health.txt"))
    assert h["hrv_baseline"] == 50
