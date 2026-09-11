"""Ende-zu-Ende: ein Task durchläuft mehrere Stufen gegen die ECHTE
Zustandsmaschine und die ECHTE Datenbank.

Warum echt und nicht gemockt: Beide Fehler, die Plan 2b behebt, haben je
acht bis neun grüne Task-Reviews überlebt, weil jeder Unit-Test die Datenbank
mockt und deshalb keiner einen Task durch mehr als eine Stufe geschickt hat.
`queue.set_state` ist ein Compare-and-Swap in SQL; ein handgeschriebener Stub
davon wäre genau die Attrappe, die in Plan 2a schon einmal einen Fehler
verdeckt hat.

Die LLM-Backends bleiben gemockt — sie kosten knappes Kontingent und sind
nicht der Prüfgegenstand.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import db
from forge import backends, models as m, pipeline as pl, queue, stages
from forge.runner import RunResult


def _db_erreichbar() -> bool:
    try:
        db.init_pool()
        db.query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_erreichbar(), reason="PostgreSQL nicht erreichbar — Lebenszyklus-Test übersprungen")


@pytest.fixture
def task_id():
    """Ein echter Task in der echten Tabelle, hinterher restlos entfernt."""
    neue_id = queue.enqueue("Lebenszyklus-Test", "wegwerf", source="test", priority=1)
    yield neue_id
    # forge_journal.task_id trägt ON DELETE CASCADE (core/db.py), die
    # Journaleinträge verschwinden also mit dem Task.
    db.execute("DELETE FROM forge_tasks WHERE id=%s", (neue_id,))


@pytest.fixture
def gemockte_backends(monkeypatch):
    """Jede Stufe gelingt; die Rückgabe ist inhaltlich leer.

    Ruling A (Controller): budget.buche/markiere_erschoepft und ketten.waehle
    bleiben hier NICHT echt, obwohl DB und Zustandsmaschine es sind. Reales
    buche() würde die `laeufe`-Zähler der produktiven forge_budget-Zeile
    dieser Nacht verbrauchen (agy hat nur ~18 Läufe/Nacht) — Prüfgegenstand
    dieses Tests ist die Zustandsmaschine, nicht das Budget. Reales
    ketten.waehle() liest budget.ist_erschoepft() aus derselben Zeile, der
    Test würde also je nachdem, ob heute Nacht schon etwas erschöpft ist,
    unterschiedlich ausfallen. Deshalb: dieselbe feste Zuordnung Stufe →
    (Backend, Modell) wie fest in forge/stages.py, keine Budget-Schreibungen.
    """
    monkeypatch.setattr(backends, "hole", lambda name: (
        lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
    monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
    monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
    monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
    monkeypatch.setattr(pl, "_committe_artefakt", lambda *a: None)
    monkeypatch.setattr(pl.budget, "buche", lambda model, tokens_in, tokens_out: None)
    monkeypatch.setattr(pl.budget, "markiere_erschoepft", lambda model, grund: None)
    _glied_je_stufe = {s.name: (s.backend, s.model) for s in stages.ALLE_STUFEN}
    monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): _glied_je_stufe.get(name))


def _zustand(task_id: int) -> str:
    return db.query_one("SELECT state FROM forge_tasks WHERE id=%s", (task_id,))["state"]


class TestLebenszyklus:
    def test_task_laeuft_von_queued_bis_gating(self, task_id, gemockte_backends, tmp_path):
        """Der Weg, den es geben muss: vier Stufen, kein Park.

        Abweichung von der Brief-Vorlage (siehe Report): `queue.enqueue`
        legt den Task in `queued` an, aber `_eine_stufe_intern` kennt diesen
        Zustand nicht — nur `queue.claim_next()` fuehrt in Produktion den
        Uebergang queued -> speccing aus. `claim_next()` selbst waere hier
        aber gefaehrlich: es zieht ungefragt den obersten QUEUED-Task ueber
        alle Prioritaeten hinweg und wuerde damit im schlimmsten Fall einen
        ECHTEN Produktions-Task anfassen statt unseres Wegwerf-Tasks. Deshalb
        wird hier gezielt nur `task_id` von QUEUED nach SPECCING gesetzt —
        exakt der Uebergang, den claim_next() fuer den obersten Task macht.
        """
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        gesehen = [_zustand(task_id)]
        for _ in range(6):
            zustand = _zustand(task_id)
            if zustand == m.GATING:
                break
            task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
            ergebnis = pl._eine_stufe_intern(task, task_id, zustand, tmp_path)
            assert ergebnis != "geparkt", f"geparkt in {zustand}"
            gesehen.append(_zustand(task_id))

        assert _zustand(task_id) == m.GATING, f"Verlauf: {gesehen}"

    def test_negatives_review_landet_in_der_fix_runde(self, task_id, gemockte_backends, monkeypatch, tmp_path):
        """Der Beweis, dass die Fix-Schleife erreichbar ist — der Fehler aus Task 2.

        Ruling B (Controller): review.json darf beim ERSTEN Aufruf noch NICHT
        existieren — _eine_stufe_intern ruft _waehle_stufe() beim Einstieg
        auf, und ein schon vorliegendes negatives Urteil würde sofort auf
        FIX_STAGE umschalten, bevor die echte Review-Stufe je gelaufen ist.
        Genau wie tests/test_forge_pipeline.py::TestFixStufeErreichbar
        (43b84a1) schreibt der gemockte Backend-Lauf das Urteil deshalb als
        SEITENEFFEKT während des Laufs — so, wie forge/runner_agy.py es in
        echt tut.
        """
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        queue.set_state(task_id, m.PLANNING, current=m.SPECCING)
        queue.set_state(task_id, m.IMPLEMENTING, current=m.PLANNING)
        queue.set_state(task_id, m.REVIEWING, current=m.IMPLEMENTING)

        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)

        def _review_lauf(prompt, cwd, timeout, agent, model):
            (tmp_path / ".forge" / "review.json").write_text(
                json.dumps({"verdict": "fail",
                            "findings": [{"severity": "critical", "what": "x"}]}))
            return RunResult(ok=True, text="egal")

        monkeypatch.setattr(pl.backends, "hole", lambda name: _review_lauf)

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        pl._eine_stufe_intern(task, task_id, m.REVIEWING, tmp_path)
        assert _zustand(task_id) == m.REVIEWING, "Review ist trotz negativem Urteil vorgerueckt"

        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is True and stufe.name == "fix"

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        pl._eine_stufe_intern(task, task_id, m.REVIEWING, tmp_path)
        assert _zustand(task_id) == m.IMPLEMENTING, "Fix-Stufe hat nicht nach IMPLEMENTING gefuehrt"

    def test_erschoepfte_kette_laesst_den_zustand_stehen(self, task_id, monkeypatch, tmp_path):
        """Kontingent darf keinen Zustand verbrennen — der Fehler aus Task 1."""
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        ergebnis = pl._eine_stufe_intern(task, task_id, m.SPECCING, tmp_path)

        assert ergebnis == "kontingent"
        assert _zustand(task_id) == m.SPECCING, "Zustand wurde trotz Kontingent veraendert"
