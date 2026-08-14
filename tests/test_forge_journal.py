"""Unit-Tests für das Ereignis-Log der Forge. DB gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import journal as j


class _Recorder:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executes: list[tuple] = []
        self.queries: list[tuple] = []

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return 1

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows


def _patch(monkeypatch, rows=None):
    rec = _Recorder(rows=rows)
    monkeypatch.setattr(j.db, "execute", rec.execute)
    monkeypatch.setattr(j.db, "query", rec.query)
    return rec


class TestLog:
    def test_schreibt_alle_felder(self, monkeypatch):
        rec = _patch(monkeypatch)
        j.log(3, "stage_done", "Spec fertig", tokens_in=100, tokens_out=250)
        assert rec.executes[-1][1] == (3, "stage_done", "Spec fertig", 100, 250)

    def test_task_id_darf_none_sein(self, monkeypatch):
        rec = _patch(monkeypatch)
        # Daemon-Ereignisse (Start, Not-Aus) hängen an keinem Task.
        j.log(None, "daemon_start", "Forge gestartet")
        assert rec.executes[-1][1][0] is None

    def test_unbekannte_art_wird_trotzdem_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        # Ein unbekannter kind darf nichts verschlucken — ein verlorenes
        # Ereignis ist schlimmer als ein unsauber benanntes.
        j.log(1, "irgendwas_neues", "Text")
        assert rec.executes[-1][1][1] == "irgendwas_neues"

    def test_defaults_sind_null_tokens(self, monkeypatch):
        rec = _patch(monkeypatch)
        j.log(1, "gate_pass")
        assert rec.executes[-1][1] == (1, "gate_pass", "", 0, 0)


def _boom(sql, params=()):
    raise RuntimeError("DB nicht erreichbar")


class TestLogSchreibfehler:
    def test_datenbankfehler_wird_nicht_weitergereicht(self, monkeypatch):
        # Der Daemon ruft journal.log() ständig auf — ein DB-Ausfall darf ihn
        # nie zum Absturz bringen. Kein try/except hier: j.log() muss selbst
        # normal zurückkehren.
        monkeypatch.setattr(j.db, "execute", _boom)
        j.log(1, "stage_done", "Text")

    def test_datenbankfehler_bleibt_sichtbar_im_log(self, monkeypatch):
        # Verschluckt werden darf der Fehler nur, wenn er sichtbar bleibt —
        # sonst wäre ein stiller Datenverlust selbst ein Fehler.
        monkeypatch.setattr(j.db, "execute", _boom)
        errors: list[str] = []
        monkeypatch.setattr(j._log, "error", lambda msg: errors.append(msg))
        j.log(1, "stage_done", "Text")
        assert len(errors) == 1
        assert "DB nicht erreichbar" in errors[0]


class TestRecent:
    def test_limit_wird_durchgereicht(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.recent(limit=5)
        assert rec.queries[-1][1] == (5,)

    def test_sortiert_absteigend_nach_zeit(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.recent()
        assert "ORDER BY ts DESC" in rec.queries[-1][0]


class TestForTask:
    def test_filtert_nach_task(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.for_task(11)
        assert rec.queries[-1][1] == (11,)

    def test_sortiert_aufsteigend(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.for_task(11)
        # Für einen einzelnen Task will man den Verlauf von vorn lesen.
        assert "ORDER BY ts ASC" in rec.queries[-1][0]


class TestKinds:
    def test_bekannte_arten_sind_dokumentiert(self, monkeypatch):
        for kind in ["stage_start", "stage_done", "gate_pass", "gate_fail",
                     "merged", "reverted", "paused", "idea_added", "daemon_start"]:
            assert kind in j.KINDS
