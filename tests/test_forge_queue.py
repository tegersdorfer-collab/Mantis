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
        # Kein Zustandswechsel, kein Schreibzugriff überhaupt: seit Plan 2 zählt
        # ein bloßer Claim nicht mehr als Versuch (siehe TestClaimNextRuehrtAttemptsNicht).
        assert rec.executes == []

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

class TestClaimNextRuehrtAttemptsNicht:
    """Kern der Korrektur (Akzeptanzlauf 2026-08-15, Task 5): ein Claim allein
    ist kein Fehlschlag mehr. Plan 1 kannte nur einen Tick pro Task; Plan 2s
    Pipeline (forge/pipeline.py) bringt einen Task pro Tick genau eine Stufe
    weiter — ein gesunder Task braucht mindestens fünf Ticks (spec, plan,
    implement, review, gate). Die alte, an claim_next() hängende Zählung
    hätte jeden gesunden Task allein durch normalen Fortschritt gegen die
    Schwelle laufen lassen; genau das geschah real (drei erfolgreiche Stufen,
    dann 'Automatisch geparkt: 4 Versuche ohne Erfolg')."""

    def test_laufender_task_schreibt_attempts_nicht_egal_wie_hoch_es_schon_steht(self, monkeypatch):
        # Würde die geprüfte Zeile (der entfernte Zähl-Aufruf in claim_next)
        # wieder eingefügt, parkte dieser Task sofort automatisch (attempts
        # steht bereits über der Schwelle) statt zurückgegeben zu werden.
        rec = _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING, "attempts": 99}])
        task = q.claim_next()
        assert task is not None
        assert task["id"] == 3
        assert task["attempts"] == 99  # unverändert durchgereicht, nicht hochgezählt
        assert rec.executes == []

    def test_frisch_aus_der_queue_geclaimter_task_schreibt_nur_den_zustand(self, monkeypatch):
        rec = _patch_seq(monkeypatch, [[], [{"id": 11, "state": m.QUEUED, "attempts": 0}]])
        q.claim_next()
        # Der einzige Schreibzugriff ist der Zustandswechsel nach SPECCING —
        # kein "SET attempts=" mehr, egal wie oft geclaimt wird.
        assert not any("attempts" in sql for sql, _ in rec.executes)

    def test_fehlende_attempts_spalte_verursacht_keinen_schreibzugriff(self, monkeypatch):
        # Bestandstasks ohne den Schlüssel im Dict dürfen nicht crashen — und
        # claim_next() darf trotzdem nichts in die Spalte schreiben.
        rec = _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING}])
        task = q.claim_next()
        assert task is not None
        assert rec.executes == []


class TestZaehleFehlschlag:
    """`zaehle_fehlschlag` ersetzt den alten, claim-basierten Zähler: jetzt
    zählt nur ein tatsächlicher Fehlschlag (siehe forge/pipeline.py und
    forge/daemon.py, Funktion `_park`)."""

    def test_erster_fehlschlag_erhoeht_nur_zaehlt_nicht_geparkt(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"attempts": 1}])
        geparkt = q.zaehle_fehlschlag(5, current=m.IMPLEMENTING)
        assert geparkt is False
        assert not any("parked_reason" in sql for sql, _ in rec.executes)

    def test_schreibt_attempts_plus_eins_mit_der_task_id(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"attempts": 1}])
        q.zaehle_fehlschlag(5, current=m.IMPLEMENTING)
        zeile = next(p for sql, p in rec.queries if "attempts+1" in sql.replace(" ", ""))
        assert zeile == (5,)

    def test_unter_der_schwelle_bleibt_der_task_aktiv(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"attempts": 3}])  # == AUTO_PARK_AFTER_ATTEMPTS
        geparkt = q.zaehle_fehlschlag(5, current=m.IMPLEMENTING)
        assert geparkt is False
        assert not any("parked_reason" in sql for sql, _ in rec.executes)

    def test_ueberschreitet_die_schwelle_wird_automatisch_geparkt(self, monkeypatch):
        # Ein Task, dessen Fehlschlag-Zähler die Schwelle übersteigt, wird
        # automatisch geparkt — Rückgabe True zeigt dem Aufrufer, dass er
        # keinen zweiten (spezifischeren) park()-Aufruf mehr nachschieben muss.
        rec = _patch(monkeypatch, rows=[{"attempts": 4}])  # > AUTO_PARK_AFTER_ATTEMPTS
        geparkt = q.zaehle_fehlschlag(5, current=m.IMPLEMENTING)
        assert geparkt is True
        park_aufrufe = [p for sql, p in rec.executes if "parked_reason" in sql]
        assert len(park_aufrufe) == 1
        assert park_aufrufe[0][0] == m.PARKED
        assert park_aufrufe[0][2] == 5
        # Der Grund muss die neue Semantik nennen — Fehlschläge, keine Claims.
        assert "Fehlschläge in Folge" in park_aufrufe[0][1]
        assert "Versuche ohne Erfolg" not in park_aufrufe[0][1]

    def test_geloeschter_task_gibt_false_statt_zu_crashen(self, monkeypatch):
        # Wenn der Task zwischen read und write gelöscht wurde (UPDATE trifft
        # keine Zeile), darf zaehle_fehlschlag nicht crashen.
        _patch(monkeypatch, rows=[])
        assert q.zaehle_fehlschlag(999, current=m.IMPLEMENTING) is False


class TestVersucheZuruecksetzen:
    def test_setzt_attempts_auf_null(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.versuche_zuruecksetzen(5)
        assert rec.executes[-1][1] == (5,)
        assert "attempts=0" in rec.executes[-1][0].replace(" ", "")

    def test_updated_at_wird_mitgezogen(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.versuche_zuruecksetzen(5)
        assert "updated_at=NOW()" in rec.executes[-1][0]


class TestFehlschlagUndErfolgZusammenspiel:
    """Verhaltens-Test aus der Aufgabenstellung: ein Erfolg ZWISCHEN zwei
    Fehlschlägen setzt den Zähler zurück, der Task wird nicht geparkt —
    ohne den Reset in `versuche_zuruecksetzen` würde der dritte Fehlschlag
    hier fälschlich die Schwelle reißen."""

    def test_erfolg_zwischen_fehlschlaegen_verhindert_das_parken(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"attempts": 1}])
        assert q.zaehle_fehlschlag(5, current=m.IMPLEMENTING) is False  # attempts -> 1

        rec.rows = []
        q.versuche_zuruecksetzen(5)  # ein sauberer Stufen-Abschluss dazwischen

        rec.rows = [{"attempts": 1}]
        assert q.zaehle_fehlschlag(5, current=m.IMPLEMENTING) is False  # wieder bei 1, nicht 2
        rec.rows = [{"attempts": 2}]
        assert q.zaehle_fehlschlag(5, current=m.IMPLEMENTING) is False  # 2, nicht 3 -> unter Schwelle
        assert not any("parked_reason" in sql for sql, _ in rec.executes)


class TestFuenfErfolgreicheStufenParkenNie:
    """Verhaltens-Test aus der Aufgabenstellung, direkt gegen die Queue: eine
    Kette aus fünf erfolgreichen Stufen (spec, plan, implement, review, gate)
    claimt denselben Task fünfmal und meldet fünfmal Erfolg. Würde claim_next
    dabei noch mitzählen (der alte Fehler), risse das die Schwelle von drei
    schon nach der vierten Stufe."""

    def test_fuenf_claims_und_erfolge_parken_nie_attempts_bleibt_null(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"id": 5, "state": m.SPECCING, "attempts": 0}])
        for _ in range(5):
            task = q.claim_next()
            assert task is not None
            q.versuche_zuruecksetzen(task["id"])
        assert not any("parked_reason" in sql for sql, _ in rec.executes)
        # Der letzte Schreibzugriff auf attempts (falls überhaupt einer
        # stattfand) muss ihn auf 0 setzen, nie hochzählen.
        attempts_schreibzugriffe = [p for sql, p in rec.executes if "attempts" in sql and "SET" in sql]
        assert all(p == (5,) for p in attempts_schreibzugriffe)


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
