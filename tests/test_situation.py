"""Fakes für den kompakten Lagebild-Snapshot; keine DB- oder Wetterzugriffe."""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from core import situation


_NOW = datetime(2026, 10, 14, 9, 30, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture(autouse=True)
def _reset_snapshot(monkeypatch):
    monkeypatch.setattr(situation, "_snapshot", None)


def test_build_snapshot_formats_each_source_and_limits_open_tasks():
    tasks = [
        {"title": f"Aufgabe {i}", "priority": "high" if i in {2, 8} else "low",
         "due": date(2026, 10, 14) if i < 8 else None, "status": "todo"}
        for i in range(9)
    ]
    snapshot = situation.build_snapshot(
        now=_NOW,
        calendar_events=[
            {"title": "Zahnarzt", "start": datetime(2026, 10, 14, 11, 0)},
            {"title": "Projekttermin", "start": datetime(2026, 10, 15, 14, 0)},
        ],
        tasks=tasks,
        health_records=[{
            "date": date(2026, 10, 14), "sleep_duration": 7.5, "sleep_deep": 1.2,
            "resting_hr": 52, "hrv": 61,
        }],
        weather={
            "city": "Nürnberg", "now": {"temp": 13, "desc": "teils bewölkt"},
            "forecast": [{"date": "2026-10-14", "min": 8, "max": 16, "code": "Regen"}],
        },
        reminder={"text": "Paket abholen", "remind_at": datetime(2026, 10, 14, 12, 0)},
        workouts=[{"title": "Krafttraining", "date": date(2026, 10, 13)}],
    )

    assert "Stand: Mittwoch, 14.10.2026, 09:30 Uhr" in snapshot.text
    assert "Zahnarzt" in snapshot.text and "Projekttermin" in snapshot.text
    assert "Aufgabe 7" in snapshot.text and "Aufgabe 8" not in snapshot.text
    assert snapshot.text.index("Aufgabe 2") < snapshot.text.index("Aufgabe 0")
    assert "7,5 h" in snapshot.text and "1,2 h" in snapshot.text
    assert "Ruhepuls 52" in snapshot.text and "HRV 61" in snapshot.text
    assert "Nürnberg" in snapshot.text and "Paket abholen" in snapshot.text
    assert "Krafttraining" in snapshot.text
    assert len(snapshot.text) <= 1200


def test_refresh_snapshot_omits_only_failed_source_and_marks_partial_result():
    def fail():
        raise RuntimeError("fake task source failure")

    sources = {
        "calendar": lambda: [],
        "tasks": fail,
        "health": lambda: [],
        "weather": lambda: {"city": "Berlin", "now": {"temp": 12, "desc": "klar"}, "forecast": []},
        "reminder": lambda: None,
        "workouts": lambda: [],
    }

    snapshot = asyncio.run(situation.refresh_snapshot(sources=sources, now=_NOW))

    assert snapshot.incomplete is True
    assert "Aufgaben:" not in snapshot.text
    assert "Termine heute: keine" in snapshot.text
    assert "Wetter Berlin" in snapshot.text


def test_missing_health_metrics_mark_snapshot_incomplete():
    snapshot = situation.build_snapshot(
        now=_NOW,
        calendar_events=[],
        tasks=[],
        health_records=[{"date": date(2026, 10, 14), "sleep_duration": 6.5}],
        weather=None,
        reminder=None,
        workouts=[],
    )

    assert "Schlaf letzte Nacht 6,5 h" in snapshot.text
    assert snapshot.incomplete is True


def test_prompt_context_reads_only_memory_and_explains_freshness(monkeypatch):
    snapshot = situation.SituationSnapshot(
        refreshed_at=_NOW,
        text="Stand: Mittwoch, 14.10.2026, 09:30 Uhr\nTermine heute: Zahnarzt",
        incomplete=False,
    )
    monkeypatch.setattr(situation, "_snapshot", snapshot)
    monkeypatch.setattr(situation.config, "SITUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(situation.config, "SITUATION_REFRESH_S", 300, raising=False)

    context = situation.prompt_context(now=_NOW)

    assert context.startswith("## Lagebild")
    assert "Zahnarzt" in context
    assert "direkt aus diesem Snapshot" in context
    assert "Stand älter" in context


def test_prompt_context_marks_partial_or_stale_snapshot_for_tool_fallback(monkeypatch):
    snapshot = situation.SituationSnapshot(
        refreshed_at=datetime(2026, 10, 14, 9, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        text="Stand: Mittwoch, 14.10.2026, 09:20 Uhr\nWetter Berlin: klar",
        incomplete=True,
    )
    monkeypatch.setattr(situation, "_snapshot", snapshot)
    monkeypatch.setattr(situation.config, "SITUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(situation.config, "SITUATION_REFRESH_S", 300, raising=False)

    context = situation.prompt_context(now=_NOW)

    assert "unvollständig" in context


def test_prompt_context_marks_old_snapshot_as_stale(monkeypatch):
    snapshot = situation.SituationSnapshot(
        refreshed_at=datetime(2026, 10, 14, 9, 19, tzinfo=ZoneInfo("Europe/Berlin")),
        text="Stand: Mittwoch, 14.10.2026, 09:19 Uhr\nTermine heute: keine",
        incomplete=False,
    )
    monkeypatch.setattr(situation, "_snapshot", snapshot)
    monkeypatch.setattr(situation.config, "SITUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(situation.config, "SITUATION_REFRESH_S", 300, raising=False)

    context = situation.prompt_context(now=_NOW)

    assert "veraltet" in context


def test_prompt_context_stales_after_refresh_window_and_idle_tick_grace(monkeypatch):
    snapshot = situation.SituationSnapshot(
        refreshed_at=datetime(2026, 10, 14, 9, 23, 59, tzinfo=ZoneInfo("Europe/Berlin")),
        text="Stand: Mittwoch, 14.10.2026, 09:23 Uhr\nTermine heute: keine",
        incomplete=False,
    )
    monkeypatch.setattr(situation, "_snapshot", snapshot)
    monkeypatch.setattr(situation.config, "SITUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(situation.config, "SITUATION_REFRESH_S", 300, raising=False)

    context = situation.prompt_context(now=_NOW)

    assert "veraltet" in context


def test_disabled_situation_returns_no_prompt_context(monkeypatch):
    monkeypatch.setattr(situation.config, "SITUATION_ENABLED", False, raising=False)

    assert situation.prompt_context(now=_NOW) == ""


def test_snapshot_truncates_unusually_large_source_data():
    events = [
        {"title": f"Termin {index} " + "x" * 120,
         "start": datetime(2026, 10, 14, index % 24, 0)}
        for index in range(30)
    ]
    tasks = [
        {"title": f"Aufgabe {index} " + "y" * 100, "priority": "medium",
         "due": date(2026, 10, 14)}
        for index in range(8)
    ]

    snapshot = situation.build_snapshot(
        now=_NOW,
        calendar_events=events,
        tasks=tasks,
        health_records=[{
            "sleep_duration": 7, "sleep_deep": 1, "resting_hr": 55, "hrv": 60,
        }],
        weather={
            "city": "Nürnberg", "now": {"temp": 13, "desc": "teils bewölkt"},
            "forecast": [{"date": "2026-10-14", "min": 8, "max": 16, "code": "Regen"}],
        },
        reminder={"text": "Erinnerung " + "z" * 100, "remind_at": datetime(2026, 10, 14, 12)},
        workouts=[{"title": "Training " + "w" * 100, "date": date(2026, 10, 13)}],
    )

    assert len(snapshot.text) <= 1200
    assert snapshot.text.endswith("…")
    assert snapshot.incomplete is True
