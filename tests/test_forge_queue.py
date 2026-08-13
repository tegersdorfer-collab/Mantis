"""Unit-Tests für die Queue der Forge. DB gestubbt — das Muster stammt aus
tests/test_tasks_status.py: das db-Modul im Ziel-Modul wird ersetzt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

from forge import models as m
from forge import queue as q


class _Recorder:
    """Nimmt SQL und Parameter auf, statt sie an Postgres zu schicken."""

    def __init__(self, rows=None, returning=1):
        self.rows = rows if rows is not None else []
        self.returning = returning
        self.queries: list[tuple] = []
        self.executes: list[tuple] = []

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows

    def query_one(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows[0] if self.rows else None

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return 1

    def insert_returning(self, sql, params=()):
        self.executes.append((sql, params))
        return self.returning


def _patch(monkeypatch, rows=None, returning=1):
    rec = _Recorder(rows=rows, returning=returning)
    for name in ("query", "query_one", "execute", "insert_returning"):
        monkeypatch.setattr(q.db, name, getattr(rec, name))
    return rec


class TestEnqueue:
    def test_gibt_neue_id_zurueck(self, monkeypatch):
        _patch(monkeypatch, returning=42)
        assert q.enqueue("X5-Weckroutine") == 42

    def test_parameter_landen_in_der_richtigen_reihenfolge(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.enqueue("Titel", "Beschreibung", source="roadmap", priority=90)
        assert rec.executes[-1][1] == ("Titel", "Beschreibung", "roadmap", 90)

    def test_default_ist_timo_mit_prio_50(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.enqueue("Titel")
        assert rec.executes[-1][1] == ("Titel", "", "timo", 50)


class TestActive:
    def test_findet_laufenden_task(self, monkeypatch):
        _patch(monkeypatch, rows=[{"id": 7, "state": m.IMPLEMENTING}])
        assert q.active()["id"] == 7

    def test_fragt_nur_nach_aktiven_zustaenden(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active()
        gefragte_zustaende = set(rec.queries[-1][1][0])
        assert gefragte_zustaende == set(m.ACTIVE_STATES)

    def test_ohne_treffer_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.active() is None

    def test_pausierte_tasks_zaehlen_nicht_als_aktiv(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active()
        # Ein Task, der bis morgen pausiert ist, darf den Daemon nicht blockieren.
        assert "paused_until IS NULL OR paused_until <= NOW()" in rec.queries[-1][0]


class TestClaimNext:
    def test_laufender_task_hat_vorrang(self, monkeypatch):
        # Wiederaufsetzen nach Absturz: kein neuer Task, solange einer offen ist.
        rec = _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING}])
        task = q.claim_next()
        assert task["id"] == 3
        assert rec.executes == []  # kein Zustandswechsel

    def test_leere_queue_gibt_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.claim_next() is None


class TestSetState:
    def test_erlaubter_uebergang_wird_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.PLANNING, current=m.SPECCING) is True
        assert rec.executes[-1][1] == (m.PLANNING, 1)

    def test_verbotener_uebergang_schreibt_nichts(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.MERGED, current=m.QUEUED) is False
        assert rec.executes == []  # lieber nichts als ein korrupter Zustand

    def test_updated_at_wird_mitgezogen(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.set_state(1, m.PLANNING, current=m.SPECCING)
        assert "updated_at=NOW()" in rec.executes[-1][0]


class TestPark:
    def test_park_schreibt_grund(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.park(5, current=m.GATING, reason="Tests rot") is True
        assert rec.executes[-1][1] == (m.PARKED, "Tests rot", 5)


class TestPause:
    def test_pause_setzt_grund_und_zeitpunkt(self, monkeypatch):
        rec = _patch(monkeypatch)
        bis = datetime.now(timezone.utc) + timedelta(hours=1)
        q.pause(9, "ratelimit", bis)
        assert rec.executes[-1][1] == ("ratelimit", bis, 9)
