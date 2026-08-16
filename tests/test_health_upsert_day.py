"""Tests für domains/health.py::upsert_day — die Änderungserkennung beim Schreiben.

upsert_day gibt jetzt zurück, ob sich der Bestand geändert hat, nicht ob geschrieben
wurde (Finding 4 im Phase-B-Review: sonst hält idle_loop._tick_health jeden 30-Minuten-
Tick für 'neue Daten', selbst wenn COROS denselben Tag unverändert zurückliefert).

Das braucht echtes Postgres-Verhalten (ON CONFLICT ... WHERE ... IS DISTINCT FROM),
und dieses Repo hat kein Testmuster für eine laufende DB — core/db.py-Tests
(tests/test_db_resilience.py) ersetzen nur den Connection-Pool, nicht die
SQL-Auswertung, und liefern für rowcount immer eine feste 1. Ein Fake, der die
Postgres-Semantik selbst nachbildet, würde nur beweisen, was der Test ohnehin
voraussetzt. Getestet wird deshalb die SQL-Konstruktion (das Prädikat deckt genau
die geschriebenen Spalten ab) und dass der Rückgabewert direkt vom rowcount aus
db.execute() abhängt. Verhaltens-Abdeckung gegen echtes Postgres steht noch aus.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains import health


def test_upsert_day_sql_has_distinct_from_guard_over_written_columns(monkeypatch):
    seen = {}

    def fake_execute(sql, params=()):
        seen["sql"] = sql
        seen["params"] = params
        return 1

    monkeypatch.setattr(health.db, "execute", fake_execute)
    health.upsert_day("2026-08-11", {"steps": 12500, "hrv": 71})

    sql = seen["sql"]
    assert "WHERE" in sql
    assert "health_data.steps IS DISTINCT FROM EXCLUDED.steps" in sql
    assert "health_data.hrv IS DISTINCT FROM EXCLUDED.hrv" in sql
    # Keine ungeschriebene Spalte taucht im Prädikat auf.
    assert "resting_hr IS DISTINCT FROM" not in sql


def test_upsert_day_returns_true_when_rowcount_positive(monkeypatch):
    # rowcount > 0: neue Zeile eingefügt ODER WHERE-Prädikat traf zu (echte Änderung).
    monkeypatch.setattr(health.db, "execute", lambda sql, params=(): 1)
    assert health.upsert_day("2026-08-11", {"steps": 1}) is True


def test_upsert_day_returns_false_when_rowcount_zero(monkeypatch):
    # rowcount 0: ON CONFLICT griff, aber IS DISTINCT FROM war für jede Spalte
    # falsch — derselbe Tag mit denselben Werten noch einmal geschrieben.
    monkeypatch.setattr(health.db, "execute", lambda sql, params=(): 0)
    assert health.upsert_day("2026-08-11", {"steps": 1}) is False


def test_upsert_day_returns_false_for_empty_fields():
    assert health.upsert_day("2026-08-11", {}) is False


def test_upsert_day_returns_false_for_empty_day():
    assert health.upsert_day("", {"steps": 1}) is False
