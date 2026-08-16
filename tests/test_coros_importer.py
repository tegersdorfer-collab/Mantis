"""Tests für den COROS-Importer (domains/coros/importer.py).

Der MCP-Client wird durch einen Fake ersetzt, der die Fixtures ausliefert; die DB
durch einen Sammel-Stub. Kein Netz, keine DB.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import importer

FIX = Path(__file__).parent / "fixtures" / "coros"

RESPONSES = {
    "queryDailyHealthData":           "daily_health.txt",
    "querySleepData":                 "sleep_data.txt",
    "querySleepHrv":                  "sleep_hrv.txt",
    "queryRestingHeartRate":          "resting_hr.txt",
    "queryAvgHeartRate":              "avg_hr.txt",
    "queryTrainingLoadAssessment":    "training_load.txt",
    "queryFitnessAssessmentOverview": "fitness_overview.txt",
    "queryRecoveryStatus":            "recovery.txt",
}


class FakeClient:
    """Liefert Fixtures im echten MCP-Result-Format. `broken` erzwingt Fehler."""

    def __init__(self, broken: set[str] | None = None):
        self.broken = broken or set()
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.broken:
            raise RuntimeError(f"{name} kaputt")
        text = (FIX / RESPONSES[name]).read_text(encoding="utf-8")
        return {"content": [{"type": "text", "text": text}]}


@pytest.fixture
def written(monkeypatch):
    """Sammelt, was upsert_day geschrieben hätte."""
    rows: dict[str, dict] = {}

    def fake_upsert(day, fields):
        if not fields:
            return False
        rows.setdefault(day, {}).update(fields)
        return True

    monkeypatch.setattr(importer.health, "upsert_day", fake_upsert)
    return rows


def test_args_clamps_the_lookback_counter():
    assert importer._args(1000, needs_range=False)["days"] == importer.MAX_LOOKBACK_DAYS


def test_args_clamps_the_derived_start_date():
    from datetime import date, timedelta
    args = importer._args(1000, needs_range=True)
    earliest = (date.today() - timedelta(days=importer.MAX_LOOKBACK_DAYS)).strftime("%Y%m%d")
    assert args["startDate"] == earliest
    assert args["days"] == importer.MAX_LOOKBACK_DAYS


def test_collect_merges_sources_into_one_day():
    days = importer.collect(FakeClient(), days=14)
    d = days["2026-08-11"]
    assert d["steps"] == 12500          # queryDailyHealthData
    assert d["sleep_score"] == 38       # querySleepData
    assert d["hrv"] == 39               # querySleepHrv
    assert d["resting_hr"] == 47        # queryRestingHeartRate
    assert d["hr_avg"] == 85            # queryAvgHeartRate
    assert d["training_load_short"] == 14


def test_collect_sends_yyyymmdd_dates_not_iso():
    c = FakeClient()
    importer.collect(c, days=14)
    for name, args in c.calls:
        for key in ("startDate", "endDate"):
            if key in args:
                assert args[key].isdigit() and len(args[key]) == 8, f"{name}.{key}={args[key]}"


def test_one_broken_tool_does_not_lose_the_others():
    days = importer.collect(FakeClient(broken={"querySleepHrv"}), days=14)
    assert days["2026-08-11"]["steps"] == 12500
    assert "hrv" not in days["2026-08-11"]


def test_error_text_from_a_tool_is_treated_as_failure(monkeypatch):
    c = FakeClient()
    err = (FIX / "error_anomalies.txt").read_text(encoding="utf-8")
    real = c.call_tool

    def call(name, arguments):
        if name == "querySleepData":
            return {"content": [{"type": "text", "text": err}]}
        return real(name, arguments)

    c.call_tool = call
    days = importer.collect(c, days=14)
    assert "sleep_score" not in days["2026-08-11"]
    assert days["2026-08-11"]["steps"] == 12500


def test_flat_metrics_land_on_the_most_recent_day():
    days = importer.collect(FakeClient(), days=14)
    newest = max(days)
    assert days[newest]["vo2max"] == 47
    assert days[newest]["recovery_pct"] == 82
    older = sorted(days)[0]
    assert "vo2max" not in days[older]


def test_sync_writes_every_day_and_counts_them(written):
    n = importer.sync(days=14, client=FakeClient())
    assert n == len(written) > 0
    assert written["2026-08-11"]["steps"] == 12500


def test_sync_without_token_returns_zero_and_does_not_raise(monkeypatch, written):
    from domains.coros import oauth

    def boom():
        raise oauth.CorosNotAuthorized("nicht autorisiert")

    monkeypatch.setattr(importer, "_build_client", boom)
    assert importer.sync(days=14) == 0
    assert written == {}


def test_sync_survives_total_network_failure(monkeypatch, written):
    class DeadClient:
        def call_tool(self, name, arguments):
            raise OSError("Netz weg")

    assert importer.sync(days=14, client=DeadClient()) == 0
    assert written == {}


def test_sync_returns_partial_count_when_writing_dies(monkeypatch):
    """Eine sterbende DB darf den Hintergrund-Tick nicht sprengen."""
    calls = {"n": 0}

    def exploding_upsert(day, fields):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("DB weg")
        return True

    monkeypatch.setattr(importer.health, "upsert_day", exploding_upsert)
    assert importer.sync(days=14, client=FakeClient()) == 2


def test_dashboard_refresh_health_calls_the_coros_importer(monkeypatch):
    """Die Naht: refresh_health muss auf COROS zeigen, nicht mehr auf den Poll."""
    from tools.dashboard import DashboardReader

    seen = {}

    def fake_sync(days=14, client=None):
        seen["days"] = days
        return 7

    monkeypatch.setattr("domains.coros.importer.sync", fake_sync)
    assert DashboardReader().refresh_health() == 7
    assert seen["days"] == 14
