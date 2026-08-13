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


class _SeqRecorder(_Recorder):
    """Wie `_Recorder`, aber `query()` liefert bei jedem Aufruf das nächste
    Ergebnis aus einer vorgegebenen Folge, statt immer dieselben `rows`.

    Wird gebraucht, wenn zwei aufeinanderfolgende SELECTs unterschiedliche
    Treffer haben müssen — z.B. `active()` leer, aber die Queue nicht."""

    def __init__(self, row_sequence, returning=1):
        super().__init__(rows=[], returning=returning)
        self._row_sequence = list(row_sequence)

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        if self._row_sequence:
            return self._row_sequence.pop(0)
        return []


def _patch_seq(monkeypatch, row_sequence, returning=1):
    rec = _SeqRecorder(row_sequence, returning=returning)
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
        # Kein Zustandswechsel — der einzige Schreibzugriff ist der Versuchszähler (C2).
        assert not any("SET state=" in sql for sql, _ in rec.executes)

    def test_leere_queue_gibt_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.claim_next() is None

    def test_beansprucht_obersten_queued_task(self, monkeypatch):
        # active() liefert nichts, die Queue hat einen wartenden Task.
        _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED, "priority": 80}]])
        task = q.claim_next()
        assert task["id"] == 11

    def test_geclaimter_task_hat_state_speccing(self, monkeypatch):
        # Nicht nur der DB-Schreibzugriff zählt — auch die In-Memory-Mutation.
        _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED}]])
        task = q.claim_next()
        assert task["state"] == m.SPECCING

    def test_schreibt_update_mit_speccing_und_task_id(self, monkeypatch):
        rec = _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED}]])
        q.claim_next()
        zustands_updates = [(sql, p) for sql, p in rec.executes if "SET state=" in sql]
        assert len(zustands_updates) == 1
        sql, params = zustands_updates[0]
        assert "UPDATE" in sql and "state=%s" in sql
        # I8: Compare-and-Swap — der erwartete Ausgangszustand steht in der WHERE-Klausel,
        # sonst könnten zwei Daemonen denselben Task per read-then-write doppelt claimen.
        assert params == (m.SPECCING, 11, m.QUEUED)

    def test_queue_sortiert_nach_prioritaet_absteigend(self, monkeypatch):
        rec = _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED}]])
        q.claim_next()
        sql = rec.queries[-1][0]
        assert "ORDER BY priority DESC" in sql

    def test_verweigerter_uebergang_gibt_none_statt_halbem_claim(self, monkeypatch):
        # Kann set_state den Übergang nicht schreiben, darf claim_next keinen
        # halb-geclaimten Task zurückgeben — lieber None als korrupter Zustand.
        _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED}]])
        monkeypatch.setattr(q, "set_state", lambda *a, **kw: False)
        assert q.claim_next() is None

    def test_versuchszaehler_wird_erhoeht(self, monkeypatch):
        # C2: jeder Claim zählt als ein Versuch — sonst kann ein Task, der
        # immer wieder abstürzt, ohne dass tick() selbst zum Parken kommt, die
        # Spitze der Queue für immer blockieren.
        rec = _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING, "attempts": 1}])
        task = q.claim_next()
        assert task["attempts"] == 2
        attempts_updates = [p for sql, p in rec.executes if "SET attempts=" in sql]
        assert attempts_updates == [(2, 3)]

    def test_fehlende_attempts_spalte_zaehlt_als_null(self, monkeypatch):
        # Bestandstasks ohne den Schlüssel im Dict (z.B. ältere Fixtures) dürfen
        # nicht crashen — 0 ist der plausible Startwert.
        _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING}])
        task = q.claim_next()
        assert task["attempts"] == 1

    def test_ueberschreitet_die_schwelle_wird_automatisch_geparkt(self, monkeypatch):
        # C2: ein Task, der die Schwelle überschreitet, wird geparkt und
        # claim_next() gibt None zurück, statt den Task ein weiteres Mal an
        # den Aufrufer zu reichen — sonst hält ein kaputter Task, dessen
        # park()-Aufruf aus tick() selbst irgendwann scheitert, die Queue fest.
        rec = _patch(monkeypatch, rows=[{"id": 5, "state": m.IMPLEMENTING, "attempts": 3}])
        assert q.claim_next() is None
        park_aufrufe = [p for sql, p in rec.executes if "parked_reason" in sql]
        assert len(park_aufrufe) == 1
        assert park_aufrufe[0][0] == m.PARKED
        assert park_aufrufe[0][2] == 5

    def test_unter_der_schwelle_bleibt_der_task_aktiv(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"id": 5, "state": m.IMPLEMENTING, "attempts": 2}])
        task = q.claim_next()
        assert task is not None
        assert task["attempts"] == 3
        assert not any("parked_reason" in sql for sql, _ in rec.executes)


class TestSetState:
    def test_erlaubter_uebergang_wird_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.PLANNING, current=m.SPECCING) is True
        # I8: CAS — Zielzustand, ID und der erwartete Ausgangszustand (WHERE state=%s).
        assert rec.executes[-1][1] == (m.PLANNING, 1, m.SPECCING)

    def test_verbotener_uebergang_schreibt_nichts(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.MERGED, current=m.QUEUED) is False
        assert rec.executes == []  # lieber nichts als ein korrupter Zustand

    def test_updated_at_wird_mitgezogen(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.set_state(1, m.PLANNING, current=m.SPECCING)
        assert "updated_at=NOW()" in rec.executes[-1][0]

    def test_verlorenes_cas_gibt_false(self, monkeypatch):
        # I8: zwei Daemonen (oder ein Handlauf neben dem launchd-Job) dürfen
        # denselben Task nicht doppelt claimen. rowcount=0 heißt: ein anderer
        # war schneller — set_state muss das als False melden, nicht als Erfolg.
        _patch(monkeypatch)
        monkeypatch.setattr(q.db, "execute", lambda sql, params=(): 0)
        assert q.set_state(1, m.PLANNING, current=m.SPECCING) is False


class TestPark:
    def test_park_schreibt_grund(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.park(5, current=m.GATING, reason="Tests rot") is True
        # I8: CAS auch hier — der erwartete Ausgangszustand steht in der WHERE-Klausel.
        assert rec.executes[-1][1] == (m.PARKED, "Tests rot", 5, m.GATING)

    def test_verlorenes_cas_gibt_false(self, monkeypatch):
        # I4 verlässt sich darauf, dass park() einen echten Fehlschlag meldet,
        # nicht nur einen theoretisch verbotenen Übergang.
        _patch(monkeypatch)
        monkeypatch.setattr(q.db, "execute", lambda sql, params=(): 0)
        assert q.park(5, current=m.GATING, reason="Tests rot") is False


class TestPause:
    def test_pause_setzt_grund_und_zeitpunkt(self, monkeypatch):
        rec = _patch(monkeypatch)
        bis = datetime.now(timezone.utc) + timedelta(hours=1)
        q.pause(9, "ratelimit", bis)
        assert rec.executes[-1][1] == ("ratelimit", bis, 9)
