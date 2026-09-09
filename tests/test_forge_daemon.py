"""Tests für die Entscheidungslogik des Forge-Daemons.

Geprüft wird, WANN gearbeitet wird — nicht ob claude funktioniert. Alle
Außenkontakte (Queue, Worktree, Pipeline, Gate) sind gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import daemon as d
from forge import models as m


@pytest.fixture
def frei(monkeypatch, tmp_path):
    """Standardlage: kein Not-Aus."""
    monkeypatch.setattr(d, "STOP_FILE", tmp_path / "kein-stop")


class TestShouldRun:
    def test_normalfall_laeuft(self, frei):
        laeuft, _ = d.should_run(failures=0)
        assert laeuft is True

    def test_not_aus_datei_stoppt(self, monkeypatch, tmp_path):
        stop = tmp_path / "stop"
        stop.write_text("")
        monkeypatch.setattr(d, "STOP_FILE", stop)
        laeuft, grund = d.should_run(failures=0)
        assert laeuft is False
        assert "Not-Aus" in grund


    def test_fehler_spirale_stoppt(self, frei):
        laeuft, grund = d.should_run(failures=d.MAX_CONSECUTIVE_FAILURES)
        assert laeuft is False
        assert "Fehlschläge" in grund

    def test_schwelle_ist_drei(self):
        assert d.MAX_CONSECUTIVE_FAILURES == 3

    def test_zwei_fehler_stoppen_noch_nicht(self, frei):
        laeuft, _ = d.should_run(failures=2)
        assert laeuft is True


class TestFehlerSpiraleUeberlebtNeustart:
    def test_spirale_setzt_die_not_aus_datei(self, monkeypatch, tmp_path):
        # Der launchd-Job läuft mit KeepAlive=true. Ohne diese Datei würde der
        # Daemon 30s nach dem Selbst-Stopp mit failures=0 neu starten und
        # dieselben Fehlläufe erneut verbrennen — die Bremse wäre keine.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", lambda: "fehler")
        d.main()
        assert stop.exists()

    def test_nach_der_spirale_laeuft_nichts_mehr(self, monkeypatch, tmp_path):
        stop = tmp_path / "stop"
        stop.write_text("Fehler-Spirale\n")
        monkeypatch.setattr(d, "STOP_FILE", stop)
        laeuft, grund = d.should_run(failures=0)
        assert laeuft is False
        assert "Not-Aus" in grund


def _task(task_id: int, state: str) -> dict:
    return {"id": task_id, "title": "Test", "description": "", "state": state}


class TestTick:
    """Ab Plan 2 delegiert tick() die eigentliche Arbeit an
    pipeline.eine_stufe() — geprüft wird hier nur noch die Verdrahtung:
    Task holen, Worktree sicherstellen, Pipeline-Rückgabe durchreichen."""

    def test_leere_queue_meldet_leerlauf(self, monkeypatch, frei):
        monkeypatch.setattr(d.queue, "claim_next", lambda: None)
        assert d.tick() == "leerlauf"

    def test_pipeline_weiter_wird_durchgereicht(self, monkeypatch, frei, tmp_path):
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(1, m.SPECCING))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, baum: "weiter")
        assert d.tick() == "weiter"

    def test_pipeline_geparkt_wird_durchgereicht(self, monkeypatch, frei, tmp_path):
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(2, m.IMPLEMENTING))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, baum: "geparkt")
        assert d.tick() == "geparkt"

    def test_pipeline_fehler_rate_limit_wird_durchgereicht(self, monkeypatch, frei, tmp_path):
        # "fehler" aus der Pipeline heißt Rate-Limit — der Zustand bleibt
        # bewusst unverändert (siehe forge/pipeline.py), tick() darf hier
        # nichts parken, nur den Backoff auslösen.
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(3, m.REVIEWING))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, baum: "fehler")
        park_aufgerufen = []
        monkeypatch.setattr(d.queue, "park", lambda *a, **kw: park_aufgerufen.append(1) or True)
        assert d.tick() == "fehler"
        assert park_aufgerufen == []


class TestGateAnschluss:
    """Meldet die Pipeline 'fertig' (Zustand jetzt GATING), lässt tick() das
    Gate laufen, journalt das Urteil mit allen Gründen und beendet den Task:
    grün -> awaiting_restart_window (bleibt liegen), rot -> parken."""

    def _vorbereitet(self, monkeypatch, tmp_path, task_id=10, ausgangszustand=m.REVIEWING):
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(task_id, ausgangszustand))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, baum: "fertig")
        # DB gestubbt: forge/daemon.py ruft bei Erfolg/Fehlschlag jetzt die
        # echten queue.py-Funktionen zaehle_fehlschlag/versuche_zuruecksetzen
        # auf — ungestubbt würden die gegen die echte Postgres-Verbindung laufen.
        monkeypatch.setattr(d.queue, "zaehle_fehlschlag", lambda tid, current: False)
        monkeypatch.setattr(d.queue, "versuche_zuruecksetzen", lambda tid: None)

    def test_gruenes_gate_geht_in_awaiting_restart(self, monkeypatch, frei, tmp_path):
        self._vorbereitet(monkeypatch, tmp_path)
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=True, gruende=[]))
        aufrufe = []
        monkeypatch.setattr(d.queue, "set_state",
                            lambda tid, target, current: aufrufe.append((tid, target, current)) or True)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        assert d.tick() == "fertig"
        assert aufrufe == [(10, m.AWAITING_RESTART, m.GATING)]

    def test_gruenes_gate_journalt_gate_pass(self, monkeypatch, frei, tmp_path):
        self._vorbereitet(monkeypatch, tmp_path)
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=True, gruende=[]))
        monkeypatch.setattr(d.queue, "set_state", lambda *a, **kw: True)
        geloggt = []
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: geloggt.append(a))
        d.tick()
        arten = [zeile[1] for zeile in geloggt]
        assert "gate_pass" in arten

    def test_rotes_gate_parkt_mit_allen_gruenden(self, monkeypatch, frei, tmp_path):
        self._vorbereitet(monkeypatch, tmp_path)
        gruende = ["Sperrzone berührt: forge/gate.py", "Tests rot: 3 failed"]
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=False, gruende=gruende))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        park_aufrufe = []
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: park_aufrufe.append((tid, current, reason)) or True)
        assert d.tick() == "geparkt"
        assert len(park_aufrufe) == 1
        tid, current, reason = park_aufrufe[0]
        assert tid == 10
        assert current == m.GATING
        assert "Sperrzone berührt: forge/gate.py" in reason
        assert "Tests rot: 3 failed" in reason

    def test_rotes_gate_journalt_gate_fail_mit_allen_gruenden(self, monkeypatch, frei, tmp_path):
        self._vorbereitet(monkeypatch, tmp_path)
        gruende = ["Grund eins", "Grund zwei"]
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=False, gruende=gruende))
        monkeypatch.setattr(d.queue, "park", lambda *a, **kw: True)
        geloggt = []
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: geloggt.append(a))
        d.tick()
        fail_zeilen = [zeile for zeile in geloggt if len(zeile) > 1 and zeile[1] == "gate_fail"]
        assert len(fail_zeilen) == 1
        nachricht = fail_zeilen[0][2]
        assert "Grund eins" in nachricht
        assert "Grund zwei" in nachricht

    def test_setstate_fehlschlag_nach_gruenem_gate_parkt(self, monkeypatch, frei, tmp_path):
        # CAS verloren (z.B. ein zweiter Schreiber war schneller) — "fertig"
        # zurückzugeben würde einen Fortschritt vorgaukeln, der nie stattfand.
        self._vorbereitet(monkeypatch, tmp_path)
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=True, gruende=[]))
        monkeypatch.setattr(d.queue, "set_state", lambda *a, **kw: False)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        park_aufrufe = []
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: park_aufrufe.append((tid, current, reason)) or True)
        assert d.tick() == "geparkt"
        assert park_aufrufe[0][:2] == (10, m.GATING)

    def test_park_false_nach_rotem_gate_wird_zusaetzlich_geloggt(self, monkeypatch, frei, tmp_path):
        # Wie forge.pipeline._park: ein verworfener park()-Aufruf darf nicht
        # spurlos bleiben, sonst hält ein Task, den nichts mehr bewegen kann,
        # die Queue fest, ohne dass irgendwo sichtbar wird warum.
        self._vorbereitet(monkeypatch, tmp_path)
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=False, gruende=["x"]))
        monkeypatch.setattr(d.queue, "park", lambda *a, **kw: False)
        geloggt = []
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: geloggt.append(a))
        d.tick()
        assert any("nicht angenommen" in str(zeile) for zeile in geloggt)

    def test_wiederaufnahme_in_gating_ueberspringt_pipeline(self, monkeypatch, frei, tmp_path):
        # Ein Absturz zwischen "fertig" und dem Gate-Lauf hinterlässt den Task
        # bereits im Zustand GATING — die Pipeline kennt diesen Zustand nicht
        # (sie deckt nur speccing..reviewing ab) und würde ohne diese
        # Sonderbehandlung mit "Kein Stufen-Handler" parken, ohne dass das
        # Gate je gelaufen wäre.
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(11, m.GATING))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)

        def _explodiert(task, baum):
            raise AssertionError("pipeline.eine_stufe hätte nicht aufgerufen werden dürfen")
        monkeypatch.setattr(d.pipeline, "eine_stufe", _explodiert)

        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=True, gruende=[]))
        monkeypatch.setattr(d.queue, "set_state", lambda *a, **kw: True)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        assert d.tick() == "fertig"


class TestFailureBackoff:
    def test_fehlschlag_bekommt_backoff_sleep(self, monkeypatch, tmp_path):
        # C2: vor dem Fix schlief main() nur im Leerlauf-Zweig — nach einem
        # Fehlschlag ging es sofort in den nächsten Tick, bis zu drei
        # Vollpreis-Claude-Läufe in Sekunden statt in gedrosseltem Abstand.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", lambda: "fehler")
        schlaefe = []
        monkeypatch.setattr(d.time, "sleep", lambda s: schlaefe.append(s))
        d.main()
        assert d.FAILURE_SLEEP_SECONDS in schlaefe



class TestTickUeberlebtAbsturz:
    """Ab Plan 2 fängt pipeline.eine_stufe() ihre eigenen Ausnahmen bereits ab
    (nie mehr eine Exception aus einem Claude-Lauf) — der verbleibende
    Sicherheitsnetz-Bedarf in tick() gilt anderen, weiterhin ungeschützten
    Aufrufen wie worktree.create()."""

    def test_ausnahme_in_worktree_create_fuehrt_nicht_zu_aktivem_task(self, monkeypatch, frei, tmp_path):
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(4, m.SPECCING))

        def _explodiert(tid, **kw):
            raise RuntimeError("Worktree-Erstellung abgestürzt")

        monkeypatch.setattr(d.worktree, "create", _explodiert)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        # DB gestubbt: tick()s Absturzpfad ruft jetzt zaehle_fehlschlag zur
        # Buchführung auf, bevor der eigentliche park()-Aufruf erfolgt.
        monkeypatch.setattr(d.queue, "zaehle_fehlschlag", lambda tid, current: False)

        geparkt = {}
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: geparkt.setdefault("aufruf", (tid, reason)) or True)

        with pytest.raises(RuntimeError):
            d.tick()

        assert geparkt["aufruf"][0] == 4
        assert "Worktree-Erstellung abgestürzt" in geparkt["aufruf"][1]

    def test_main_faengt_tick_absturz_ab_und_zaehlt_als_fehler(self, monkeypatch, tmp_path):
        # main() darf bei einer Ausnahme aus tick() weder sterben noch sie
        # verschlucken — sie muss als Fehlschlag zählen, sonst schützt die
        # Fehler-Spirale-Bremse nicht vor einem kaputten Runner.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.time, "sleep", lambda *a, **kw: None)

        geloggt = []
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: geloggt.append(a))

        def _explodiert():
            raise RuntimeError("kaputt")

        monkeypatch.setattr(d, "tick", _explodiert)
        d.main()

        # Nach drei abgefangenen Abstürzen muss die Spirale greifen und die
        # Not-Aus-Datei setzen — genau wie bei drei regulären "fehler"-Rückgaben.
        assert stop.exists()
        gruende = [zeile[2] for zeile in geloggt if len(zeile) > 2]
        assert any("Tick-Absturz" in str(g) for g in gruende)

    def test_end_zu_end_worktree_absturz_ueber_main_faengt_und_zaehlt(self, monkeypatch, tmp_path):
        # Geschlossene Kette, nicht nur tick() isoliert: worktree.create() wirft,
        # main() ruft echtes tick() auf (kein Stub für tick selbst) — der
        # Absturz muss trotzdem über main()s except aufgefangen werden, sonst
        # stirbt der ganze Daemon-Prozess an einem einzigen kaputten Lauf.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(9, m.SPECCING))
        # C2: tick() parkt jetzt real (echtes Modul, kein Stub für tick selbst) —
        # ohne diesen Stub würde queue.park() gegen die echte (nicht verbundene) DB laufen.
        monkeypatch.setattr(d.queue, "park", lambda *a, **kw: True)
        monkeypatch.setattr(d.queue, "zaehle_fehlschlag", lambda tid, current: False)

        def _worktree_explodiert(tid, **kw):
            raise RuntimeError("Worktree-Erstellung abgestürzt")

        monkeypatch.setattr(d.worktree, "create", _worktree_explodiert)

        # main() darf hierbei nicht selbst mit der RuntimeError aus dem Test fliegen.
        d.main()
        assert stop.exists()
