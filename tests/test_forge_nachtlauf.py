"""Der Nachtlauf-Simulator: das ECHTE daemon.main() über viele Ticks gegen die
ECHTE Datenbank, mit Störungen nach Drehbuch.

Warum: Drei Pläne in Folge (1, 2a, 2b) haben Task-Reviews grün gesehen, was
das Abschluss-Review als Critical fand — Rate-Limit-Arithmetik über Ticks,
veraltete Artefakte nach einem Absturz, Runden, die ohne Lauf verbraucht
wurden. Kein Unit-Test sieht so etwas, weil keiner mehr als einen Tick
kennt. Dieser Test kennt die ganze Nacht.

Gemockt sind nur die Ränder: worktree.create (-> tmp_path), gate.pruefe,
time.sleep (zählt statt zu warten), die Fenster-Uhr und die LLM-Backends —
durch ein Drehbuch, das je Aufruf sagt, was passiert. Alles dazwischen
(queue, set_state-CAS, _waehle_stufe, Kettenwahl, Rundenzählung, die
Sleep-Kette in main) ist echt.

Die Kettenwahl ist echt, nur budget.ist_erschoepft ist ein Set: so bleibt
die Anbieter-Arithmetik (Erschöpfung gilt anbieterweit) im Test dieselbe wie
in der Nacht.
"""
import functools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import db
from forge import budget, daemon as d, gate, ketten, models as m, pipeline as pl, queue, stages
from forge.runner import RunResult


def _db_erreichbar() -> bool:
    try:
        db.init_pool()
        db.query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_erreichbar(), reason="PostgreSQL nicht erreichbar — Nachtlauf übersprungen")


class _NachtEnde(BaseException):
    """Hält main() nach N Ticks an. BaseException, damit kein except Exception
    im Daemon sie schluckt."""


class _Absturz(BaseException):
    """Der Prozess stirbt an genau dieser Zeile. Wie _NachtEnde, aber der
    Test startet main() danach erneut — das ist der Neustart."""


class Drehbuch:
    """Antworten je Backend-Aufruf, in Reihenfolge. Ein Eintrag ist ein String
    ('ok', 'rate_limit', 'fehler', 'verdikt_pass', 'verdikt_fail') oder eine
    Funktion (agent, cwd) -> RunResult. Ist das Drehbuch leer, kommt 'ok'."""

    def __init__(self, *schritte):
        self.schritte = list(schritte)
        self.aufrufe: list[str] = []

    def __call__(self, prompt, cwd, timeout, agent, model):
        self.aufrufe.append(agent)
        schritt = self.schritte.pop(0) if self.schritte else "ok"
        if callable(schritt):
            return schritt(agent, Path(cwd))
        if schritt == "rate_limit":
            return RunResult(ok=False, rate_limited=True, error="429")
        if schritt == "fehler":
            return RunResult(ok=False, error="kaputt")
        if schritt in ("verdikt_pass", "verdikt_fail"):
            assert agent == "review", f"Verdikt-Schritt, aber Stufe ist '{agent}'"
            ok = schritt == "verdikt_pass"
            datei = Path(cwd) / stages.VERDIKT_DATEI
            datei.parent.mkdir(parents=True, exist_ok=True)
            datei.write_text(json.dumps({
                "verdict": "pass" if ok else "fail",
                "findings": [] if ok else [{"severity": "critical", "what": "x"}]}))
            return RunResult(ok=True, text="egal")
        return RunResult(ok=True, text="egal")


@pytest.fixture
def nacht(monkeypatch, tmp_path):
    """Eine leere Nacht: Testtasks weg, Ränder gemockt, Zähler bei null.

    Liefert ein Objekt mit `.tasks(n)`, `.drehbuch`, `.erschoepft`,
    `.laufen(max_ticks)`, `.schlaefe`, `.zustand(id)`.
    """
    db.execute("DELETE FROM forge_tasks WHERE source=%s", (queue.TEST_QUELLE,))

    class _Nacht:
        pass

    n = _Nacht()
    n.ids = []
    n.drehbuch = Drehbuch()
    n.erschoepft: set[str] = set()
    n.schlaefe: list[int] = []
    n.ticks = 0
    n.fenster = [True]  # letzter Wert gilt weiter

    def tasks(anzahl):
        for i in range(anzahl):
            n.ids.append(queue.enqueue(f"Nachtlauf-Test {i}", "wegwerf",
                                       source=queue.TEST_QUELLE, priority=100 - i))
        return list(n.ids)
    n.tasks = tasks

    def zustand(task_id):
        return db.query_one("SELECT state, refusals FROM forge_tasks WHERE id=%s", (task_id,))
    n.zustand = zustand

    def baum(task_id):
        return tmp_path / f"task-{task_id}"
    n.baum = baum

    # Ränder
    monkeypatch.setattr(d.db, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(d.db, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(d, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
    monkeypatch.setattr(d, "im_nachtfenster",
                        lambda jetzt=None: n.fenster.pop(0) if len(n.fenster) > 1 else n.fenster[0])
    monkeypatch.setattr(d.queue, "claim_next", functools.partial(queue.claim_next, quelle=queue.TEST_QUELLE))
    monkeypatch.setattr(d.worktree, "create",
                        lambda task_id: (baum(task_id).mkdir(parents=True, exist_ok=True) or baum(task_id)))
    monkeypatch.setattr(d.gate, "pruefe", lambda baum, basis="main": gate.GateErgebnis(ok=True))
    monkeypatch.setattr(d.time, "sleep", lambda s: n.schlaefe.append(s))
    # Journalzeilen ohne task_id (daemon_start/-stop) landen sonst als Waisen
    # in der Produktionstabelle — forge_journal.task_id NULL hat keinen CASCADE.
    echtes_log = d.journal.log
    monkeypatch.setattr(d.journal, "log",
                        lambda tid, *a, **k: echtes_log(tid, *a, **k) if tid is not None else None)

    monkeypatch.setattr(pl.backends, "hole", lambda name: n.drehbuch)
    monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
    monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
    monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
    monkeypatch.setattr(pl, "_committe_artefakt", lambda *a: None)
    monkeypatch.setattr(pl.budget, "buche", lambda model, tokens_in, tokens_out: None)
    monkeypatch.setattr(pl.budget, "markiere_erschoepft",
                        lambda model, grund: n.erschoepft.add(budget.provider_von_modell(model)))
    # Kettenwahl ECHT — nur die Erschöpfungsfrage kommt aus dem Set.
    monkeypatch.setattr(ketten.budget, "ist_erschoepft",
                        lambda model: budget.provider_von_modell(model) in n.erschoepft)

    echter_tick = d.tick

    def _tick():
        n.ticks += 1
        if n.ticks > n.max_ticks:
            raise _NachtEnde
        return echter_tick()
    monkeypatch.setattr(d, "tick", _tick)

    def laufen(max_ticks):
        """main() bis max_ticks oder bis es von selbst endet. Rückgabe:
        'nacht_ende' (Tick-Limit), 'beendet' (main kehrte zurück),
        'absturz' (ein Absturz-Schritt hat den Prozess getötet)."""
        n.max_ticks = n.ticks + max_ticks
        try:
            d.main()
            return "beendet"
        except _NachtEnde:
            return "nacht_ende"
        except _Absturz:
            return "absturz"
    n.laufen = laufen

    yield n

    db.execute("DELETE FROM forge_tasks WHERE source=%s", (queue.TEST_QUELLE,))


def _invarianten(n):
    """Was nach JEDER Nacht gelten muss, egal was das Drehbuch tat."""
    assert not d.STOP_FILE.exists(), "Not-Aus-Datei geschrieben"
    for tid in n.ids:
        z = n.zustand(tid)
        assert z["refusals"] <= pl.MAX_FIXRUNDEN, f"Task {tid}: {z['refusals']} Fix-Runden"
        assert z["state"] in m._TRANSITIONS, f"Task {tid}: unbekannter Zustand {z['state']}"
        verdikt = n.baum(tid) / stages.VERDIKT_DATEI
        if z["state"] == m.REVIEWING and verdikt.is_file():
            # Ein Urteil in REVIEWING muss aus einem Review-Lauf stammen, der
            # NACH dem letzten Implement/Fix lag — sonst ist es veraltet.
            letzte_arbeit = max((k for k, a in enumerate(n.drehbuch.aufrufe) if a in ("implement", "fix")), default=-1)
            letztes_review = max((k for k, a in enumerate(n.drehbuch.aufrufe) if a == "review"), default=-1)
            assert letztes_review > letzte_arbeit, f"Task {tid}: veraltetes Urteil in REVIEWING"


class TestGuteNacht:
    def test_zwei_tasks_landen_zur_freigabe(self, nacht):
        a, b = nacht.tasks(2)
        nacht.drehbuch = Drehbuch(*(["ok", "ok", "ok", "verdikt_pass"] * 2))
        ergebnis = nacht.laufen(max_ticks=20)
        assert ergebnis == "nacht_ende"
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.zustand(b)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review"] * 2
        _invarianten(nacht)


class TestFixSchleifeInDerNacht:
    def test_negatives_review_wird_gefixt_und_dann_freigegeben(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=12)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review", "fix", "review"], \
            "nach dem Fix muss direkt ein Review folgen — kein zweites Implement"
        assert nacht.zustand(a)["refusals"] == 1
        _invarianten(nacht)

    def test_zwei_negative_reviews_parken_ohne_dritten_fix(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_fail", "ok", "verdikt_fail")
        nacht.laufen(max_ticks=15)
        z = nacht.zustand(a)
        assert z["state"] == m.PARKED
        assert z["refusals"] == pl.MAX_FIXRUNDEN
        assert nacht.drehbuch.aufrufe.count("fix") == pl.MAX_FIXRUNDEN
        _invarianten(nacht)

    def test_rate_limit_im_fix_verbraucht_keine_runde(self, nacht):
        """Fund I4 (2b): vorher parkte der Task nach zwei Rate-Limits im Fix
        mit 'Fix-Runden-Grenze erreicht', ohne dass je ein Fix lief."""
        (a,) = nacht.tasks(1)
        # implement-Kette: minimax (nvidia) -> nemotron (nvidia) -> kimi (nvidia) -> gemini (google).
        # Ein nvidia-Rate-Limit nimmt drei Glieder auf einmal; dann bleibt gemini.
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "rate_limit", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=15)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.zustand(a)["refusals"] == 1
        assert nacht.drehbuch.aufrufe.count("fix") == 2
        _invarianten(nacht)


class TestKontingentInDerNacht:
    def test_rate_limit_kaskade_legt_die_forge_nicht_still(self, nacht):
        """Fund I1 (2b): zwei Rate-Limits nacheinander leeren beide Anbieter
        der Implement-Kette. Erwartet: 'kontingent', Zustand steht, keine
        Not-Aus-Datei — und nach dem Budget-Reset geht es weiter."""
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "rate_limit", "rate_limit", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=6)
        assert nacht.zustand(a)["state"] == m.IMPLEMENTING, "Kontingent hat den Zustand verbrannt"
        # Die Sleep-Kette der Nacht ist das Signal, nicht nur ihr Vorkommen: das
        # erste Rate-Limit (gemini bleibt) ist ein legitimer Fehler-Backoff, das
        # zweite leert die Kette und MUSS als Kontingent zählen. Zählte es als
        # zweiter "fehler", stünde hier [30, 30, 900, 900] — und ein dritter
        # Fehler gleich welcher Art schriebe die Not-Aus-Datei (Fund I1).
        assert nacht.schlaefe == [d.FAILURE_SLEEP_SECONDS] + [d.KONTINGENT_SLEEP_SECONDS] * 3, \
            "das Rate-Limit, das die Kette leert, zählt in die Fehler-Spirale"
        assert not d.STOP_FILE.exists()
        assert nacht.erschoepft == {"nvidia", "google"}
        # Nächste Nacht: Budget zurück
        nacht.erschoepft.clear()
        nacht.laufen(max_ticks=6)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        _invarianten(nacht)

    def test_drei_kontingent_ticks_schreiben_keine_not_aus_datei(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.erschoepft.update({"nvidia", "google"})
        nacht.laufen(max_ticks=5)
        assert not d.STOP_FILE.exists()
        assert nacht.zustand(a)["state"] == m.SPECCING
        assert nacht.schlaefe.count(d.KONTINGENT_SLEEP_SECONDS) >= 3
        _invarianten(nacht)


class TestAbsturzInDerNacht:
    def test_absturz_nach_dem_fix_vor_der_zaehlung_fuehrt_zum_review(self, nacht, monkeypatch):
        """Fund I3 (2b), am Fix: stirbt der Prozess, nachdem das alte Urteil
        verworfen ist, darf der nächste Tick NICHT erneut fixen."""
        (a,) = nacht.tasks(1)
        echtes_zaehlen = pl.queue.zaehle_fixrunde
        einmal = {"gestorben": False}

        def _zaehlen_dann_sterben(task_id):
            if not einmal["gestorben"]:
                einmal["gestorben"] = True
                raise _Absturz
            return echtes_zaehlen(task_id)
        monkeypatch.setattr(pl.queue, "zaehle_fixrunde", _zaehlen_dann_sterben)

        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_pass")
        assert nacht.laufen(max_ticks=10) == "absturz"
        # Neustart
        nacht.laufen(max_ticks=10)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review", "fix", "review"]
        _invarianten(nacht)

    def test_absturz_mitten_in_einer_stufe_parkt_und_die_nacht_geht_weiter(self, nacht):
        """Der bestehende Vertrag von tick(): eine Ausnahme aus einem Lauf
        parkt den Task. Der zweite Task kommt trotzdem dran."""
        a, b = nacht.tasks(2)

        def _explodiert(agent, cwd):
            raise RuntimeError("Backend kaputt")
        nacht.drehbuch = Drehbuch("ok", _explodiert, "ok", "ok", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=12)
        assert nacht.zustand(a)["state"] == m.PARKED
        assert nacht.zustand(b)["state"] == m.AWAITING_APPROVAL
        assert not d.STOP_FILE.exists()
        _invarianten(nacht)


class TestFensterende:
    def test_fensterende_laesst_den_task_aktiv_und_die_naechste_nacht_setzt_auf(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_pass")
        nacht.fenster = [True, True, False]  # zwei Ticks, dann 07:00
        assert nacht.laufen(max_ticks=10) == "beendet"
        assert nacht.zustand(a)["state"] == m.IMPLEMENTING
        assert nacht.ticks == 2
        nacht.fenster = [True]
        nacht.laufen(max_ticks=10)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        _invarianten(nacht)

    def test_weicher_stop_beendet_nach_dem_laufenden_tick(self, nacht, monkeypatch):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_pass")
        gesehen = []
        zaehlender_tick = d.tick  # der Zähl-Wrapper der Fixture

        def _tick_dann_halt():
            r = zaehlender_tick()
            gesehen.append(r)
            d.HALT_FILE.write_text("stop")
            return r
        monkeypatch.setattr(d, "tick", _tick_dann_halt)

        assert nacht.laufen(max_ticks=10) == "beendet"
        assert gesehen == ["weiter"]
        assert nacht.zustand(a)["state"] == m.PLANNING
        assert not d.HALT_FILE.exists()
        _invarianten(nacht)
