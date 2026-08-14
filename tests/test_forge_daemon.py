"""Tests für die Entscheidungslogik des Forge-Daemons.

Geprüft wird, WANN gearbeitet wird — nicht ob claude funktioniert. Alle
Außenkontakte (Queue, Worktree, Runner) sind gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import daemon as d
from forge import models as m
from forge.runner import RunResult


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


class TestTick:
    def test_leere_queue_meldet_leerlauf(self, monkeypatch, frei):
        monkeypatch.setattr(d.queue, "claim_next", lambda: None)
        assert d.tick() == "leerlauf"

    def test_erfolgreicher_lauf_parkt_den_task(self, monkeypatch, frei, tmp_path):
        # Plan 1 merged noch nicht — jeder Task endet als Trockenlauf geparkt.
        aufrufe = {}
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 1, "title": "Test", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=True, text="fertig",
                                                                        tokens_in=5, tokens_out=7))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: aufrufe.setdefault("park", (tid, reason)))
        assert d.tick() == "trockenlauf"
        assert aufrufe["park"][0] == 1

    def test_fehlgeschlagener_lauf_parkt_mit_grund(self, monkeypatch, frei, tmp_path):
        aufrufe = {}
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 2, "title": "Test", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=False, error="kaputt"))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: aufrufe.setdefault("park", (tid, reason)))
        assert d.tick() == "fehler"
        assert "kaputt" in aufrufe["park"][1]

    def test_journal_bekommt_die_tokens(self, monkeypatch, frei, tmp_path):
        # Ohne diese Zahlen kann Plan 3 kein Budget führen.
        gemerkt = []
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 3, "title": "T", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=True, text="x",
                                                                        tokens_in=11, tokens_out=22))
        monkeypatch.setattr(d.queue, "park", lambda tid, current, reason: True)
        monkeypatch.setattr(d.journal, "log",
                            lambda *a, **kw: gemerkt.append((kw.get("tokens_in"), kw.get("tokens_out"))))
        d.tick()
        assert (11, 22) in gemerkt


class TestParkRueckgabeWirdGeprueft:
    """I4: tick() verwarf früher den Rückgabewert von queue.park() komplett.
    Lieferte park() False (verbotener Übergang, verlorenes CAS), gab tick()
    trotzdem "trockenlauf" zurück — failures würde auf 0 zurückgesetzt, und es
    gäbe keinen Backoff-Sleep: eine ungebremste Schleife voller Vollpreis-Läufe
    an einem Task, der laut DB in Wahrheit aktiv hängen blieb."""

    def test_park_false_nach_erfolgreichem_lauf_wird_als_fehler_gewertet(self, monkeypatch, frei, tmp_path):
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 6, "title": "T", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=True, text="x"))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "park", lambda tid, current, reason: False)
        assert d.tick() == "fehler"


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
    def test_ausnahme_im_runner_fuehrt_nicht_zu_aktivem_task(self, monkeypatch, frei, tmp_path):
        # C2: tick() fängt eine Ausnahme aus dem Lauf jetzt selbst ab, parkt den
        # Task VOR dem Weiterreichen (mit der echten Task-ID, nie None) und
        # wirft danach weiter — main()s eigenes except zählt sie zusätzlich als
        # Fehlschlag. Vor dem Fix hätte runner.run() einfach durchgeschlagen,
        # OHNE dass queue.park() je aufgerufen wurde: der Task wäre in
        # ACTIVE_STATES hängen geblieben und beim nächsten Tick sofort wieder
        # gezogen worden — dreimal hintereinander, ohne jeden Backoff.
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 4, "title": "T", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)

        def _explodiert(prompt, cwd, timeout=1800):
            raise RuntimeError("claude-Prozess abgestürzt")

        monkeypatch.setattr(d.runner, "run", _explodiert)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)

        geparkt = {}
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: geparkt.setdefault("aufruf", (tid, reason)) or True)

        with pytest.raises(RuntimeError):
            d.tick()

        assert geparkt["aufruf"][0] == 4
        assert "claude-Prozess abgestürzt" in geparkt["aufruf"][1]

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

    def test_end_zu_end_runner_absturz_ueber_main_faengt_und_zaehlt(self, monkeypatch, tmp_path):
        # Geschlossene Kette, nicht nur tick() isoliert: runner.run() wirft,
        # main() ruft echtes tick() auf (kein Stub für tick selbst) — der
        # Absturz muss trotzdem über main()s except aufgefangen werden, sonst
        # stirbt der ganze Daemon-Prozess an einem einzigen kaputten Lauf.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        monkeypatch.setattr(d.time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 9, "title": "T", "description": "", "state": m.SPECCING})
        # C2: tick() parkt jetzt real (echtes Modul, kein Stub für tick selbst) —
        # ohne diesen Stub würde queue.park() gegen die echte (nicht verbundene) DB laufen.
        monkeypatch.setattr(d.queue, "park", lambda *a, **kw: True)
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)

        def _runner_explodiert(prompt, cwd, timeout=1800):
            raise RuntimeError("claude-Prozess abgestürzt")

        monkeypatch.setattr(d.runner, "run", _runner_explodiert)

        # main() darf hierbei nicht selbst mit der RuntimeError aus dem Test fliegen.
        d.main()
        assert stop.exists()
