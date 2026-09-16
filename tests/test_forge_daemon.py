"""Tests für die Entscheidungslogik des Forge-Daemons.

Geprüft wird, WANN gearbeitet wird — nicht ob claude funktioniert. Alle
Außenkontakte (Queue, Worktree, Pipeline, Gate) sind gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

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
        # launchd startet den Job per Kalender erst wieder um NACHT_BEGINN_STUNDE
        # Uhr. Ohne diese Datei würde der Daemon in der nächsten Nacht mit
        # failures=0 neu starten und dieselben Fehlläufe erneut verbrennen —
        # die Bremse wäre keine.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
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
    grün -> awaiting_approval (wartet auf Timos Freigabe), rot -> parken."""

    def _vorbereitet(self, monkeypatch, tmp_path, task_id=10, ausgangszustand=m.REVIEWING):
        monkeypatch.setattr(d.queue, "claim_next", lambda: _task(task_id, ausgangszustand))
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, baum: "fertig")
        # DB gestubbt: forge/daemon.py ruft bei Erfolg/Fehlschlag jetzt die
        # echten queue.py-Funktionen zaehle_fehlschlag/versuche_zuruecksetzen
        # auf — ungestubbt würden die gegen die echte Postgres-Verbindung laufen.
        monkeypatch.setattr(d.queue, "zaehle_fehlschlag", lambda tid, current: False)
        monkeypatch.setattr(d.queue, "versuche_zuruecksetzen", lambda tid: None)

    def test_gruenes_gate_geht_in_awaiting_approval(self, monkeypatch, frei, tmp_path):
        self._vorbereitet(monkeypatch, tmp_path)
        monkeypatch.setattr(d.gate, "pruefe", lambda baum, **kw: d.gate.GateErgebnis(ok=True, gruende=[]))
        aufrufe = []
        monkeypatch.setattr(d.queue, "set_state",
                            lambda tid, target, current: aufrufe.append((tid, target, current)) or True)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        assert d.tick() == "fertig"
        assert aufrufe == [(10, m.AWAITING_APPROVAL, m.GATING)]

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
        # Ohne diesen Stub schrieb der Test bei jedem Lauf still
        # `attempts=0` für Task 11 in die echte forge_tasks — aufgefallen erst,
        # als das Gate die Suite mit umgeleiteter DATABASE_URL ausführte.
        monkeypatch.setattr(d.queue, "versuche_zuruecksetzen", lambda tid: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        assert d.tick() == "fertig"


class TestFailureBackoff:
    def test_fehlschlag_bekommt_backoff_sleep(self, monkeypatch, tmp_path):
        # C2: vor dem Fix schlief main() nur im Leerlauf-Zweig — nach einem
        # Fehlschlag ging es sofort in den nächsten Tick, bis zu drei
        # Vollpreis-Claude-Läufe in Sekunden statt in gedrosseltem Abstand.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", lambda: "fehler")
        schlaefe = []
        monkeypatch.setattr(d.time, "sleep", lambda s: schlaefe.append(s))
        d.main()
        assert d.FAILURE_SLEEP_SECONDS in schlaefe

    def test_geparkter_task_bekommt_backoff_sleep(self, monkeypatch, tmp_path):
        # Abschluss-Review C2 (2026-09-10): main() schlief nur bei "leerlauf"
        # und "fehler". Ein Park ist aber kein Fortschritt und kann ganz ohne
        # LLM-Lauf entstehen — ohne Bremse zog der nächste Tick sofort den
        # nächsten Task, legte Worktree und Branch an, parkte ihn, und so
        # weiter durch den kompletten Backlog, in Sekunden.
        class _Abbruch(BaseException):
            """BaseException, damit main()s `except Exception` sie NICHT fängt —
            sonst liefe die Schleife weiter."""

        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)

        ergebnisse = iter(["geparkt"])

        def _tick():
            for wert in ergebnisse:
                return wert
            raise _Abbruch

        monkeypatch.setattr(d, "tick", _tick)
        schlaefe = []
        monkeypatch.setattr(d.time, "sleep", lambda s: schlaefe.append(s))
        with pytest.raises(_Abbruch):
            d.main()
        assert d.PARK_SLEEP_SECONDS in schlaefe, \
            f"kein Backoff nach einem Park: {schlaefe}"



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
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
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
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
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


class TestKontingentImDaemon:
    def test_kontingent_zaehlt_nicht_in_die_fehler_spirale(self, monkeypatch, tmp_path):
        """Drei Kontingent-Ticks dürfen den Not-Aus NICHT auslösen.

        STOP_FILE wird hier (wie in TestFehlerSpiraleUeberlebtNeustart) durch
        einen echten tmp_path-Pfad ersetzt statt seine Methoden zu stubben:
        unter Python 3.14 verweigert pathlib.Path das Setzen von
        Instanz-Attributen (`AttributeError: 'PosixPath' object attribute
        'exists' is read-only') — monkeypatch.setattr auf STOP_FILE.exists
        selbst scheitert also, bevor der eigentliche Testfall geprüft wird."""
        gesehen = {"schlaefe": []}
        ticks = iter(["kontingent"] * 5)

        class _Halt(BaseException):
            pass

        def _tick():
            try:
                return next(ticks)
            except StopIteration:
                raise _Halt

        def _sleep(s):
            gesehen["schlaefe"].append(s)

        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", _sleep)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)

        try:
            d.main()
        except _Halt:
            pass

        assert not stop.exists(), "Not-Aus trotz reinem Kontingent-Grund"
        assert gesehen["schlaefe"], "Kontingent-Tick hat gar nicht geschlafen"
        assert all(s == d.KONTINGENT_SLEEP_SECONDS for s in gesehen["schlaefe"])

    def test_fehler_loest_den_not_aus_weiterhin_aus(self, monkeypatch, tmp_path):
        """Die Spirale bleibt für echte Fehler erhalten."""
        ticks = iter(["fehler"] * 5)

        class _Halt(BaseException):
            pass

        def _tick():
            try:
                return next(ticks)
            except StopIteration:
                raise _Halt

        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", lambda s: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)

        try:
            d.main()
        except _Halt:
            pass

        assert stop.exists()


class TestNachtfenster:
    def test_23_uhr_ist_drin(self):
        assert d.im_nachtfenster(datetime(2026, 9, 14, 23, 0)) is True

    def test_3_uhr_ist_drin(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 3, 30)) is True

    def test_7_uhr_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 7, 0)) is False

    def test_mittag_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 12, 0)) is False

    def test_22_59_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 14, 22, 59)) is False


class TestFensterUndHaltImMain:
    def _main_mit(self, monkeypatch, tmp_path, fenster, halt_datei_da, ticks, tick_callback=None):
        """Lässt main() laufen; `fenster` ist die Folge der Antworten von
        im_nachtfenster(), `ticks` zählt die tick()-Aufrufe. `tick_callback`
        (optional) wird nach jedem tick()-Aufruf mit der bisherigen
        Tick-Anzahl aufgerufen — damit lässt sich z.B. die Halt-Datei
        mitten im Lauf anlegen (ein "echter" Stop, während der Daemon läuft,
        im Gegensatz zu einer bereits beim Start vorhandenen, veralteten
        Datei)."""
        antworten = iter(fenster)

        class _Halt(BaseException):
            pass

        def _tick():
            ticks.append(1)
            if tick_callback is not None:
                tick_callback(len(ticks))
            if len(ticks) > 10:
                raise _Halt
            return "leerlauf"

        halt = tmp_path / "halt"
        if halt_datei_da:
            halt.write_text("stop")
        monkeypatch.setattr(d, "HALT_FILE", halt)
        monkeypatch.setattr(d, "STOP_FILE", tmp_path / "stop")
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: next(antworten))
        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", lambda s: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        try:
            d.main()
            return "beendet", halt
        except _Halt:
            return "laeuft_noch", halt

    def test_ausserhalb_des_fensters_kein_tick(self, monkeypatch, tmp_path):
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [False], False, ticks)
        assert ergebnis == "beendet"
        assert ticks == []

    def test_fensterende_beendet_nach_dem_laufenden_tick(self, monkeypatch, tmp_path):
        """Die laufende Stufe läuft zu Ende; erst der nächste Durchlauf prüft
        das Fenster."""
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [True, True, False], False, ticks)
        assert ergebnis == "beendet"
        assert len(ticks) == 2

    def test_live_halt_beendet_nach_dem_laufenden_tick_und_wird_geloescht(self, monkeypatch, tmp_path):
        """Ein Halt, der WÄHREND eines laufenden Ticks angefordert wird
        (forge.cli stop mitten im Lauf), beendet erst den nächsten
        Schleifendurchlauf — die laufende Stufe läuft zu Ende, exakt wie
        beim Nachtfenster. Anders als eine beim Start bereits vorhandene
        Datei (siehe test_veraltete_halt_datei_beim_start_wird_entfernt)
        ist das kein Altlast-Fall, sondern eine echte, laufende Anfrage."""
        ticks = []

        def _stop_waehrend_erstem_tick(anzahl):
            if anzahl == 1:
                (tmp_path / "halt").write_text("stop")

        ergebnis, halt = self._main_mit(
            monkeypatch, tmp_path, [True, False], False, ticks,
            tick_callback=_stop_waehrend_erstem_tick,
        )
        assert ergebnis == "beendet"
        assert ticks == [1]
        assert not halt.exists(), "Halt-Datei überlebt das Beenden"

    def test_veraltete_halt_datei_beim_start_wird_entfernt(self, monkeypatch, tmp_path):
        """Eine Halt-Datei, die schon VOR main() existiert, ist eine
        Anfrage an einen Daemon, der nie gestartet ist — sie stammt aus
        einem Absturz/SIGKILL vor dem Löschen (oder, vor diesem Fix, aus
        einem Fensterausgang, der sie überleben ließ). Timos Stop am Tag
        darf die kommende Nacht nicht canceln, also entfernt der Start sie
        stillschweigend, statt sofort zu beenden — der Daemon arbeitet
        stattdessen ganz normal weiter."""
        ticks = []
        ergebnis, halt = self._main_mit(monkeypatch, tmp_path, [True] * 20, True, ticks)
        assert ergebnis == "laeuft_noch", "eine veraltete Halt-Datei darf den Lauf nicht sofort beenden"
        assert len(ticks) == 11
        assert not halt.exists()

    def test_ohne_halt_und_im_fenster_laeuft_es(self, monkeypatch, tmp_path):
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [True] * 20, False, ticks)
        assert ergebnis == "laeuft_noch"
        assert len(ticks) == 11

    def test_reihenfolge_fenster_und_halt_vor_should_run(self, monkeypatch, tmp_path):
        """Fenster- und Halt-Prüfung müssen VOR should_run() laufen: mit
        gesetzter Not-Aus-Datei UND ausserhalb des Fensters muss main()
        sofort per return enden, ohne je in den should_run()-Zweig
        (sleep(BLOCKED_SLEEP_SECONDS)) zu geraten. Eine vertauschte
        Reihenfolge (should_run() zuerst) würde stattdessen endlos in der
        300s-Pause hängen — der sleep-Stub bricht das nach drei Aufrufen
        kontrolliert mit einer eigenen Ausnahme ab, statt den Testlauf
        aufzuhängen."""
        class _Haengt(BaseException):
            pass

        ticks = []
        schlaefe = []

        def _sleep(s):
            schlaefe.append(s)
            if len(schlaefe) > 3:
                raise _Haengt

        def _tick():
            ticks.append(1)
            return "leerlauf"

        stop = tmp_path / "stop"
        stop.write_text("Fehler-Spirale\n")
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: False)
        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", _sleep)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)

        try:
            d.main()
            ergebnis = "beendet"
        except _Haengt:
            ergebnis = "haengt_in_should_run"

        assert ergebnis == "beendet"
        assert ticks == []


class TestApiSchluesselAusDatei:
    """Erste Nacht (2026-09-14, 23:00): launchd sourct keine .zshrc, die
    Schlüssel aus ~/.config/ai-keys.env fehlten dem Daemon, opencode meldete
    'Method doesn't allow unregistered callers', Task 365 parkte nach drei
    Sekunden. Der Daemon lädt die Datei deshalb selbst."""

    def _datei(self, tmp_path, inhalt):
        p = tmp_path / "ai-keys.env"
        p.write_text(inhalt)
        return p

    def test_laedt_export_und_nackte_zuweisungen(self, monkeypatch, tmp_path):
        p = self._datei(tmp_path, 'export GEMINI_API_KEY="abc"\nNVIDIA_API_KEY=xyz\n# Kommentar\n\n')
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        geladen = d.lade_api_schluessel(p)
        assert geladen == ["GEMINI_API_KEY", "NVIDIA_API_KEY"]
        import os
        assert os.environ["GEMINI_API_KEY"] == "abc"
        assert os.environ["NVIDIA_API_KEY"] == "xyz"

    def test_ueberschreibt_keine_vorhandene_variable(self, monkeypatch, tmp_path):
        p = self._datei(tmp_path, "GROQ_API_KEY=aus_datei\n")
        monkeypatch.setenv("GROQ_API_KEY", "aus_umgebung")
        assert d.lade_api_schluessel(p) == []
        import os
        assert os.environ["GROQ_API_KEY"] == "aus_umgebung"

    def test_fehlende_datei_ist_kein_fehler(self, tmp_path):
        assert d.lade_api_schluessel(tmp_path / "gibt-es-nicht") == []


class TestAbschlussMeldung:
    """Nachtrag 3a: jedes Ende von main() schickt den Morgenbericht per
    Telegram; die Fehler-Spirale sofort, mit dem Grund in der ersten Zeile.
    melden.sende ist gepatcht — kein Telegram aus Tests."""

    def _vorbereiten(self, monkeypatch, tmp_path, gesendet):
        monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(d, "STOP_FILE", tmp_path / "stop")
        monkeypatch.setattr(d.time, "sleep", lambda s: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d, "lade_api_schluessel", lambda *a, **kw: [])
        monkeypatch.setattr(d.melden, "sende", lambda text: gesendet.append(text) or True)
        from forge import bericht
        monkeypatch.setattr(bericht, "morgenbericht", lambda: "BERICHT\n")

    def test_fensterende_schickt_den_bericht_genau_einmal(self, monkeypatch, tmp_path):
        gesendet = []
        self._vorbereiten(monkeypatch, tmp_path, gesendet)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: False)
        d.main()
        assert gesendet == ["BERICHT\n"]

    def test_weicher_halt_schickt_den_bericht(self, monkeypatch, tmp_path):
        gesendet = []
        self._vorbereiten(monkeypatch, tmp_path, gesendet)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        # Halt mitten im Lauf: nach dem ersten Tick (eine vorher vorhandene
        # Datei gälte als veraltet und würde beim Start entfernt).
        ticks = []

        def _tick():
            ticks.append(1)
            (tmp_path / "halt").write_text("stop")
            return "leerlauf"

        monkeypatch.setattr(d, "tick", _tick)
        d.main()
        assert ticks == [1]
        assert gesendet == ["BERICHT\n"]

    def test_fehler_spirale_meldet_sofort_mit_grund_zuerst(self, monkeypatch, tmp_path):
        gesendet = []
        self._vorbereiten(monkeypatch, tmp_path, gesendet)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)
        monkeypatch.setattr(d, "tick", lambda: "fehler")
        d.main()
        assert len(gesendet) == 1
        erste_zeile = gesendet[0].splitlines()[0]
        assert erste_zeile.startswith("Forge abgeschaltet:")
        assert "BERICHT" in gesendet[0]

    def test_bericht_kaputt_meldet_trotzdem(self, monkeypatch, tmp_path):
        """Ein Fehler beim Bericht darf weder den Daemon-Ausgang noch die
        Meldung verhindern — dann kommt eben der Fehler als Text."""
        gesendet = []
        self._vorbereiten(monkeypatch, tmp_path, gesendet)
        from forge import bericht

        def _kaputt():
            raise RuntimeError("DB weg")

        monkeypatch.setattr(bericht, "morgenbericht", _kaputt)
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: False)
        d.main()
        assert len(gesendet) == 1
        assert "DB weg" in gesendet[0]

    def test_main_laedt_auch_die_env_datei(self, monkeypatch, tmp_path):
        """TELEGRAM_CHAT_ID steht in .env, nicht in ai-keys.env. Ohne
        diesen zweiten Ladevorgang bliebe melden.sende stumm."""
        gesendet = []
        self._vorbereiten(monkeypatch, tmp_path, gesendet)
        geladen = []
        monkeypatch.setattr(d, "lade_api_schluessel", lambda datei=None: geladen.append(datei) or [])
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: False)
        d.main()
        assert geladen == [d.API_SCHLUESSEL_DATEI, d.ENV_DATEI]
