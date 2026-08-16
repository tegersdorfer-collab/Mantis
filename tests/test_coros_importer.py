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
    "queryUserInfo":                  "user_info.txt",
}


class FakeClient:
    """Liefert Fixtures im echten MCP-Result-Format. `broken` erzwingt Fehler."""

    def __init__(self, broken: set[str] | None = None):
        self.broken = broken or set()
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.broken:
            raise RuntimeError(f"{name} kaputt")
        text = (FIX / RESPONSES[name]).read_text(encoding="utf-8")
        return {"content": [{"type": "text", "text": text}]}

    def close(self):
        self.closed = True


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


def test_collect_logs_day_count_per_source(caplog):
    with caplog.at_level("INFO", logger="mantis.coros"):
        importer.collect(FakeClient(), days=14)
    lines = [r.message for r in caplog.records if "queryDailyHealthData" in r.message]
    assert len(lines) == 1
    assert "4" in lines[0]      # daily_health.txt hat 4 Tage


def test_collect_logs_zero_days_when_a_source_yields_nothing(caplog):
    # Der Produktionsfall, der drei Metriken lautlos verschwinden ließ: eine
    # Quelle liefert erkennbare Struktur, aber keinen einzigen Tag mit Werten —
    # bisher stand davon gar nichts im Log, nur die DB blieb leer.
    c = FakeClient()
    real = c.call_tool

    def call(name, arguments):
        if name == "querySleepData":
            return {"content": [{"type": "text", "text": "2026-08-10\nSleep Score: No data\n"}]}
        return real(name, arguments)

    c.call_tool = call
    with caplog.at_level("INFO", logger="mantis.coros"):
        importer.collect(c, days=14)
    lines = [r.message for r in caplog.records if "querySleepData" in r.message]
    assert len(lines) == 1
    assert "0" in lines[0]


def test_flat_metrics_land_on_the_most_recent_day():
    days = importer.collect(FakeClient(), days=14)
    newest = max(days)
    assert days[newest]["weight"] == 61.4
    older = sorted(days)[0]
    assert "weight" not in days[older]


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
    """Eine sterbende DB darf den Hintergrund-Tick nicht sprengen.

    Jeder Tag wird einzeln gefangen (Finding 7): die ersten zwei Tage schreiben
    erfolgreich, alle folgenden schlagen fehl (calls['n'] bleibt > 2) — macht 2.
    """
    calls = {"n": 0}

    def exploding_upsert(day, fields):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("DB weg")
        return True

    monkeypatch.setattr(importer.health, "upsert_day", exploding_upsert)
    assert importer.sync(days=14, client=FakeClient()) == 2


def test_sync_logs_write_failures_once_not_per_day(monkeypatch, caplog):
    """Eine tote DB darf nicht eine Warnung je Tag erzeugen (94 Zeilen im Log)."""
    def exploding_upsert(day, fields):
        raise RuntimeError("DB weg")

    monkeypatch.setattr(importer.health, "upsert_day", exploding_upsert)
    with caplog.at_level("WARNING", logger="mantis.coros"):
        n = importer.sync(days=14, client=FakeClient())
    assert n == 0
    write_failure_lines = [r for r in caplog.records if "nicht geschrieben" in r.message]
    assert len(write_failure_lines) == 1


def test_sync_closes_a_client_it_built_itself(monkeypatch, written):
    fake = FakeClient()
    monkeypatch.setattr(importer, "_build_client", lambda: fake)
    importer.sync(days=14)
    assert fake.closed is True


def test_sync_does_not_close_an_injected_client(written):
    fake = FakeClient()
    importer.sync(days=14, client=fake)
    assert fake.closed is False


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


# ── neue Quellen und die Prioritätsregel ────────────────────────────────────

@pytest.fixture(autouse=True)
def assessed(monkeypatch):
    """Sammelt, was upsert_assessment geschrieben hätte."""
    rows: dict[str, dict] = {}

    def fake_upsert(day, fields):
        if not fields:
            return False
        rows.setdefault(day, {}).update(fields)
        return True

    monkeypatch.setattr(importer.health, "upsert_assessment", fake_upsert)
    return rows


@pytest.fixture(autouse=True)
def stored(monkeypatch):
    """Sammelt, was db.set_setting geschrieben hätte."""
    kv: dict[str, object] = {}
    monkeypatch.setattr(importer.db, "set_setting", lambda k, v: kv.__setitem__(k, v))
    return kv


def test_exact_durations_win_over_ratios_derived_from_percentages():
    # Beide Quellen liefern 2026-08-10: queryDailyHealthData exakt (Deep 1h30),
    # querySleepData gerechnet (22 % von 7h5 = 1.56). Die exakte muss gewinnen.
    days = importer.collect(FakeClient(), days=14)
    assert days["2026-08-10"]["sleep_deep"] == 1.5


def test_derived_stages_fill_days_the_exact_source_does_not_cover(monkeypatch):
    # queryDailyHealthData kennt 2026-08-20 ohne Sleep-Block, querySleepData
    # liefert dort Anteile — die gerechneten Phasen müssen die Lücke füllen.
    texts = {
        "querySleepData": (
            "Sleep Data\n====\n\n2026-08-20\nSleep Score: 70\n"
            "Main Sleep: 8h 0min\nDeep Sleep Ratio: 25%\n"
        ),
        "queryDailyHealthData": "--- 20260820 ---\nSteps: 1,000 | Calories: 100 kcal\n",
    }

    class OnlySleepAndDaily(FakeClient):
        def call_tool(self, name, arguments):
            if name not in texts:
                raise RuntimeError("in diesem Test nicht benutzt")
            return {"content": [{"type": "text", "text": texts[name]}]}

    days = importer.collect(OnlySleepAndDaily(), days=14)
    assert days["2026-08-20"]["sleep_deep"] == 2.0     # 25 % von 8 h
    assert days["2026-08-20"]["steps"] == 1000


def test_weight_from_user_info_lands_on_the_newest_day(written):
    importer.sync(days=14, client=FakeClient())
    newest = max(written)
    assert written[newest]["weight"] == 61.4


def test_profile_fields_go_to_settings_not_into_health_data(written, stored):
    importer.sync(days=14, client=FakeClient())
    assert stored["coros_height_cm"] == 168
    assert stored["coros_birthday"] == "1994-11-02"
    assert stored["coros_gender"] == "Female"
    assert stored["coros_nickname"] == "Testkonto"
    assert not any("height_cm" in f for f in written.values())


def test_assessment_goes_to_its_own_table(written, assessed):
    importer.sync(days=14, client=FakeClient())
    day = assessed[max(assessed)]
    assert day["vo2max"] == 47
    assert day["recovery_pct"] == 82
    assert day["recovery_level"] == "Moderate training allowed"
    assert day["race_marathon_sec"] == 13800
    assert day["hrv_baseline"] == 50


def test_assessment_fields_stay_out_of_health_data(written, assessed):
    importer.sync(days=14, client=FakeClient())
    assert not any("vo2max" in f or "recovery_pct" in f for f in written.values())
