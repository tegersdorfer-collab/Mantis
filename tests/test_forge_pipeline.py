"""Die Stufenmaschine. Alle Außenkontakte gestubbt — geprüft wird die
Entscheidungslogik: wann geht es weiter, wann wird geparkt, wann in die
Fix-Runde."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import subprocess

import pytest

from forge import models as m
from forge import pipeline as pl
from forge.runner import RunResult

# Die echte gitctl.run, festgehalten BEVOR die stubs-Fixture sie ersetzt.
# Tests, die gegen ein echtes temporäres Repo laufen, holen sie sich damit
# zurück (siehe TestC3GegenEchtesGit).
_echtes_gitctl = pl.gitctl.run


@pytest.fixture
def stubs(monkeypatch):
    """Sammelt alle Seiteneffekte, statt sie auszuführen."""
    # "profile" ist mit der Backend-Registry entfallen — die Pipeline reicht
    # kein PermissionProfile mehr durch, sondern den Stufennamen als "agenten".
    aufz = {"states": [], "parks": [], "journal": [], "artefakte": [], "agenten": [], "fixrunden": 0,
            "gitctl": [], "fehlschlaege": [], "zuruecksetzungen": []}

    monkeypatch.setattr(pl.queue, "set_state",
                        lambda tid, target, current: aufz["states"].append((tid, target)) or True)
    monkeypatch.setattr(pl.queue, "park",
                        lambda tid, current, reason: aufz["parks"].append((tid, reason)) or True)
    monkeypatch.setattr(pl.queue, "setze_artefakt",
                        lambda tid, feld, pfad: aufz["artefakte"].append((feld, pfad)))
    # merke_implement_modell() (Task 8) würde sonst bei implement/fix ebenfalls
    # die echte Postgres-Verbindung öffnen. Tests, die das Verhalten selbst
    # prüfen, überschreiben diesen Stub lokal (siehe TestKettenwahl).
    monkeypatch.setattr(pl.queue, "merke_implement_modell",
                        lambda tid, model: aufz.setdefault("implement_modelle", []).append((tid, model)))
    monkeypatch.setattr(pl.journal, "log",
                        lambda *a, **kw: aufz["journal"].append((a, kw)))
    # DB gestubbt (siehe Moduldocstring): ohne diese beiden Stubs riefe
    # forge/pipeline.py bei jedem Erfolg/Fehlschlag die ECHTEN queue.py-
    # Funktionen auf, die wiederum die reale Postgres-Verbindung öffnen —
    # nicht bloß crashen, sondern echte forge_tasks-Zeilen mutieren.
    monkeypatch.setattr(pl.queue, "zaehle_fehlschlag",
                        lambda tid, current: aufz["fehlschlaege"].append((tid, current)) or False)
    monkeypatch.setattr(pl.queue, "versuche_zuruecksetzen",
                        lambda tid: aufz["zuruecksetzungen"].append(tid))
    # Budget ebenso gestubbt: buche()/markiere_erschoepft() (Task 7) würden
    # sonst bei jedem Lauf die echte Postgres-Verbindung öffnen. Tests, die
    # das Budget-Verhalten selbst prüfen, überschreiben diese Stubs lokal
    # (siehe TestBudgetMeldung._budget_stub).
    monkeypatch.setattr(pl.budget, "buche", lambda model, tokens_in, tokens_out: None)
    # ketten.waehle() (Task 8) würde ohne Stub bei jedem Lauf über
    # budget.ist_erschoepft() ebenfalls die echte Postgres-Verbindung öffnen.
    # Default hier: dasselbe Glied, das vor der Kette fest in forge/stages.py
    # stand — so bleiben alle Tests, denen die Kettenwahl selbst egal ist,
    # unverändert gültig. Tests, die die Kette prüfen, überschreiben diesen
    # Stub lokal (siehe TestKettenwahl).
    _stufe_ketten_default = {s.name: (s.backend, s.model) for s in pl.stages.ALLE_STUFEN}
    monkeypatch.setattr(pl.ketten, "waehle",
                        lambda name, verboten=frozenset(): _stufe_ketten_default.get(name))
    monkeypatch.setattr(pl.budget, "markiere_erschoepft", lambda model, grund: None)

    def _fixrunde(tid):
        aufz["fixrunden"] += 1
        return aufz["fixrunden"]
    monkeypatch.setattr(pl.queue, "zaehle_fixrunde", _fixrunde)
    # Nachtrag 2c: die Grenzprüfung liest nur, gezählt wird nach dem Lauf.
    monkeypatch.setattr(pl.queue, "fixrunden", lambda tid: aufz["fixrunden"])

    # Default: jeder gitctl-Aufruf gelingt. Tests, die ein Diff oder einen
    # gescheiterten Commit brauchen, überschreiben pl.gitctl.run lokal — das
    # hier ist nur der Fallback für Tests, die kein echtes Git-Repo unter
    # tmp_path haben (spec/plan committen jetzt ihr Artefakt, siehe Fund 3).
    def _gitctl_erfolg(*args, cwd=None, timeout=300):
        aufz["gitctl"].append((args, cwd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(pl.gitctl, "run", _gitctl_erfolg)
    return aufz


def _lauf(aufz, ergebnis):
    """Ersetzt die von backends.hole(...) gelieferte run-Funktion und merkt
    sich den übergebenen Agent-Namen (die stufenspezifische Identität, mit
    der das Backend seine Rechte je Lauf konfiguriert — siehe
    forge/runner_opencode.baue_config)."""
    def _run(prompt, cwd, timeout=1800, agent=None, model=None):
        aufz["agenten"].append(agent)
        return ergebnis
    return _run


def _task(state, **extra):
    basis = {"id": 5, "title": "Testaufgabe", "description": "", "state": state}
    basis.update(extra)
    return basis


def _verdikt(worktree, verdict, findings=()):
    (worktree / ".forge").mkdir(parents=True, exist_ok=True)
    (worktree / ".forge" / "review.json").write_text(
        json.dumps({"verdict": verdict, "findings": list(findings)}))


class TestErfolgreicheStufe:
    def test_geht_in_den_naechsten_zustand(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        assert pl.eine_stufe(_task(m.PLANNING), tmp_path) == "weiter"
        assert stubs["states"][-1] == (5, m.IMPLEMENTING)

    def test_journalt_stage_done(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.PLANNING), tmp_path)
        assert any("stage_done" in str(e) for e in stubs["journal"])

    def test_letzte_kettenstufe_meldet_fertig(self, monkeypatch, stubs, tmp_path):
        # Kritischer Fund 2: dieser Test lief ursprünglich gegen tmp_path, kein
        # git-Repo — `git diff` schlägt dort fehl, und die frühere nachsichtige
        # Lesart von _schreibe_diff schluckte das (leerer, aber "erfolgreicher"
        # Diff). Nach der Korrektur parkt ein fehlgeschlagenes `git diff`
        # stattdessen den Task. Der Zweck dieses Tests war nie, das zu prüfen —
        # er soll belegen, dass die Kette bei GATING "fertig" meldet. Deshalb
        # wird gitctl.run hier gestubbt (wie in TestDiffArtefakt), damit ein
        # erfolgreicher, leerer Diff genau das simuliert, was ein echtes Repo
        # ohne Änderungen liefern würde.
        _verdikt(tmp_path, "pass")

        def _fake_gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _fake_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        assert pl.eine_stufe(_task(m.REVIEWING), tmp_path) == "fertig"

    def test_stufenname_wird_als_agent_durchgereicht(self, monkeypatch, stubs, tmp_path):
        # Seit Task 7 reicht die Pipeline kein Rechteprofil (forge/runner.py,
        # Claude-Code-Backend) mehr an den Aufruf durch — das war der frühere
        # Sicherheitsmechanismus aus Task 1/3. Die Rechte kommen jetzt aus dem
        # Backend selbst (forge/runner_opencode.baue_config); was die Pipeline
        # weiterreichen MUSS, ist die Stufenidentität (agent=stufe.name), denn
        # ohne sie liefe jede Stufe unter derselben austauschbaren Rolle.
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="x")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert stubs["agenten"][-1] == "spec"


class TestArtefaktPflicht:
    def test_fehlendes_artefakt_ist_ein_fehlschlag(self, monkeypatch, stubs, tmp_path):
        # Der Kern dieser Klasse: ein Modell, das "fertig" sagt, ohne die Datei
        # geschrieben zu haben, darf nicht durchkommen.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="docs/specs/x-design.md")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: False)
        assert pl.eine_stufe(_task(m.SPECCING), tmp_path) == "geparkt"
        assert stubs["parks"]

    def test_spec_pfad_wird_hinterlegt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="docs/specs/x-design.md")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]


class TestFehlschlag:
    def test_fehlgeschlagener_lauf_parkt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=False, error="kaputt")))
        assert pl.eine_stufe(_task(m.PLANNING), tmp_path) == "geparkt"
        assert "kaputt" in stubs["parks"][-1][1]

    def test_ratelimit_parkt_nicht(self, monkeypatch, stubs, tmp_path):
        # Plan 3 hängt hier seine Pause ein. Würde ein Rate-Limit parken, wäre
        # jede Kontingentgrenze ein verlorener Task statt einer Wartezeit.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error="usage limit", rate_limited=True)))
        assert pl.eine_stufe(_task(m.PLANNING), tmp_path) == "fehler"
        assert stubs["parks"] == []
        assert stubs["states"] == []


class TestFixRunden:
    def test_negatives_verdikt_geht_in_die_fix_stufe(self, monkeypatch, stubs, tmp_path):
        _verdikt(tmp_path, "fail", [{"severity": "important", "what": "x"}])
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="gefixt")))
        ergebnis = pl.eine_stufe(_task(m.REVIEWING), tmp_path)
        assert ergebnis == "weiter"
        # Die Fix-Stufe ist an ihrem Agent-Namen 'fix' erkennbar (kein
        # Rechteprofil mehr, siehe test_stufenname_wird_als_agent_durchgereicht).
        assert stubs["agenten"][-1] == "fix"

    def test_nach_max_fixrunden_wird_geparkt(self, monkeypatch, stubs, tmp_path):
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="x")))
        stubs["fixrunden"] = pl.MAX_FIXRUNDEN
        assert pl.eine_stufe(_task(m.REVIEWING), tmp_path) == "geparkt"

    def test_grenze_ist_zwei(self):
        assert pl.MAX_FIXRUNDEN == 2


class TestUnbekannterZustand:
    def test_zustand_ohne_stufe_parkt_statt_abzustuerzen(self, monkeypatch, stubs, tmp_path):
        assert pl.eine_stufe(_task(m.QUEUED), tmp_path) == "geparkt"


# ---------------------------------------------------------------------------
# Zusätzliche Abdeckung über den Brief hinaus.
# ---------------------------------------------------------------------------


class TestVerweigerteWerkzeuge:
    """Messung aus Task 1: ein verweigertes Tool setzt is_error NICHT — der
    Lauf sieht wie ein Erfolg aus. Ohne Auswertung von `denials` sähe eine zu
    eng berechtigte Stufe exakt so aus wie eine, deren Artefakt fehlt. Dieser
    Test prüft genau die Unterscheidung: gleicher ok=True, einmal mit und
    einmal ohne denials — die Park-Gründe müssen sich unterscheiden."""

    def test_denials_parken_mit_anderem_grund_als_fehlendes_artefakt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="docs/specs/x-design.md",
                                                    denials=[{"tool_name": "Write"}])))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: False)
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        grund_mit_denial = stubs["parks"][-1][1]
        assert "Write" in grund_mit_denial

        stubs["parks"].clear()
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="docs/specs/x-design.md")))
        ergebnis2 = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis2 == "geparkt"
        grund_ohne_denial = stubs["parks"][-1][1]

        assert grund_mit_denial != grund_ohne_denial
        assert "Write" not in grund_ohne_denial

    def test_park_grund_nennt_auch_den_fehlertext(self, monkeypatch, stubs, tmp_path):
        # I5 (Abschluss-Review): ergebnis.error wurde auf diesem Pfad nirgends
        # ausgegeben. Wer morgens den geparkten Task ansieht, erfuhr weder,
        # welches Werkzeug fehlte, noch woran der Anbieter sich gestört hat.
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(
            stubs, RunResult(ok=False, error="Tool call validation failed: apply_patch",
                             denials=[{"tool_name": "apply_patch", "message": "..."}])))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path) == "geparkt"
        grund = stubs["parks"][-1][1]
        assert "apply_patch" in grund
        assert "Tool call validation failed" in grund

    def test_mehrere_verweigerte_werkzeuge_werden_alle_genannt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="x",
                                                    denials=[{"tool_name": "Bash"}, {"tool_name": "Edit"}])))
        pl.eine_stufe(_task(m.PLANNING), tmp_path)
        grund = stubs["parks"][-1][1]
        assert "Bash" in grund and "Edit" in grund


class TestDiffArtefakt:
    """Die Review-Stufe hat kein Bash und kann sich keinen eigenen Diff
    erzeugen — die Pipeline muss .forge/diff.patch VOR dem Review-Lauf
    befüllen, sonst prüft Review eine Spec ohne jede Sicht auf die Änderung."""

    def test_schreibt_diff_vor_dem_review_lauf(self, monkeypatch, stubs, tmp_path):
        aufgezeichnet = {}

        def _fake_gitctl(*args, cwd=None, timeout=300):
            aufgezeichnet["args"] = args
            aufgezeichnet["cwd"] = cwd

            class _R:
                returncode = 0
                stdout = "+ Zeile geaendert\n"
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _fake_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        pl.eine_stufe(_task(m.REVIEWING), tmp_path)

        diff_datei = tmp_path / pl.stages.DIFF_DATEI
        assert diff_datei.is_file()
        assert diff_datei.read_text() == "+ Zeile geaendert\n"
        assert aufgezeichnet["cwd"] == tmp_path
        # Der Diff wird VOR dem Lauf geschrieben — die Stufe muss ihn schon
        # vorfinden können.
        assert len(stubs["agenten"]) == 1

    def test_diff_schreiben_schlaegt_fehl_dann_kein_review_lauf(self, monkeypatch, stubs, tmp_path):
        # Ein Worktree ohne Schreibrecht simuliert einen fehlschlagenden
        # Schreibversuch, ohne die CLI oder Dateisystem-Interna zu mocken.
        kaputt = tmp_path / "wt"
        kaputt.mkdir()
        kaputt.chmod(0o500)
        try:
            monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
            ergebnis = pl.eine_stufe(_task(m.REVIEWING), kaputt)
            assert ergebnis == "geparkt"
            # Review darf gar nicht erst gelaufen sein — sonst prüfte es einen
            # veralteten oder fehlenden Diff.
            assert stubs["agenten"] == []
            assert stubs["parks"]
        finally:
            kaputt.chmod(0o700)

    def test_fix_stufe_braucht_keinen_diff(self, monkeypatch, stubs, tmp_path):
        # Nur die eigentliche Review-Stufe braucht den vorgebauten Diff: sie
        # bekommt ihn vom Runner in den Prompt gelegt. Die Fix-Stufe arbeitet
        # gegen das Verdikt und braucht ihn nicht.
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])

        def _explodierendes_gitctl(*args, cwd=None, timeout=300):
            if args and args[0] == "diff":
                raise AssertionError("git diff haette hier nicht aufgerufen werden duerfen")

            # status/add/commit gehören seit C3 zur Fix-Stufe dazu: sie hat
            # kein Bash und kann ihre Arbeit nicht selbst committen.
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _explodierendes_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="x")))
        ergebnis = pl.eine_stufe(_task(m.REVIEWING), tmp_path)
        assert ergebnis == "weiter"


class TestRatelimitZustandBleibtUnveraendert:
    def test_kein_journal_fuer_denials_bei_ratelimit_noetig_aber_kein_park(self, monkeypatch, stubs, tmp_path):
        # Ergänzt den Brief-Test: auch wenn ein rate-limitierter Lauf
        # (theoretisch) denials trüge, darf er niemals parken.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error="usage limit",
                                                    rate_limited=True, denials=[{"tool_name": "Write"}])))
        assert pl.eine_stufe(_task(m.PLANNING), tmp_path) == "fehler"
        assert stubs["parks"] == []
        assert stubs["states"] == []


class TestTokenWeiterleitung:
    def test_alle_token_felder_landen_unverfaelscht_im_journal(self, monkeypatch, stubs, tmp_path):
        # Gemessen: ein Lauf hatte 3 Input- und 5 Output-Token. Kleine Werte
        # werden in Kürzungen oder Defaults leicht verschluckt oder vertauscht
        # — Plan 3 rechnet mit genau diesen Feldern das Budget.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="x", tokens_in=3, tokens_out=5)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        treffer = [e for e in stubs["journal"] if e[1].get("tokens_in") == 3 and e[1].get("tokens_out") == 5]
        assert treffer, f"tokens_in/out nicht korrekt weitergereicht: {stubs['journal']}"

    def test_cache_felder_landen_unverfaelscht_im_journal(self, monkeypatch, stubs, tmp_path):
        # Gemessen (tests/fixtures/claude_stream_success.jsonl): 10102 cache_read
        # und 8779 cache_creation gegen nur 3/5 tokens_in/out — Plan 3s
        # Budget-Reserve rechnet mit den Cache-Feldern, nicht nur mit tokens_in/out.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="x", cache_read=10102, cache_creation=8779)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        treffer = [e for e in stubs["journal"]
                   if e[1].get("cache_read") == 10102 and e[1].get("cache_creation") == 8779]
        assert treffer, f"cache_read/cache_creation nicht korrekt weitergereicht: {stubs['journal']}"


class TestFehlgeschlagenerGitDiffParkt:
    """Kritischer Fund 2: `git diff` mit non-zero returncode (kaputter Ref,
    kaputtes Worktree) darf nicht als leerer, aber 'erfolgreicher' Diff
    durchgehen — sonst könnte die Review-Stufe ein Urteil über eine Änderung
    fällen, die sie nie gesehen hat, und dieses Urteil füttert das Gate."""

    def test_git_diff_fehlschlag_parkt_statt_review_laufen_zu_lassen(self, monkeypatch, stubs, tmp_path):
        def _kaputtes_gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 128
                stdout = ""
                stderr = "fatal: bad revision 'main...HEAD'"
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _kaputtes_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        ergebnis = pl.eine_stufe(_task(m.REVIEWING), tmp_path)
        assert ergebnis == "geparkt"
        # Review darf gar nicht erst gelaufen sein.
        assert stubs["agenten"] == []
        grund = stubs["parks"][-1][1]
        assert "git diff" in grund
        assert "128" in grund


class TestKritisch1FixRundeErzwingtErneutesReview:
    """Kritischer Fund 1, reproduziert über zwei aufeinanderfolgende
    eine_stufe()-Aufrufe an EINEM Worktree.

    Nachtrag 2c angepasst (siehe Report, Abschnitt "Angepasste Tests"): vor
    2c brauchte es drei Ticks (fix -> implement -> review), weil der Fix nach
    IMPLEMENTING ging und die Implement-Stufe ein zweites Mal auf dem
    fertigen Baum lief. Seit 2c bleibt der Fix in REVIEWING, also sind es nur
    noch zwei Ticks:

    Tick 1: REVIEWING mit bereits vorliegendem negativem (Vor-Fix-)Urteil
            -> Fix-Stufe läuft, Task bleibt in REVIEWING (kein Übergang).
    Tick 2: Ohne den Fix in Task 6 fände _hat_negatives_verdikt dasselbe
            Vor-Fix-Urteil noch vor und schickte den Task sofort wieder in
            die Fix-Stufe, OHNE dass Review je gelaufen wäre. Mit dem Fix
            muss Tick 2 die echte Review-Stufe anfordern.
    """

    def test_zweiter_tick_laeuft_review_stufe_nicht_fix_stufe(self, monkeypatch, stubs, tmp_path):
        _verdikt(tmp_path, "fail", [{"severity": "important", "what": "x"}])

        def _fake_gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 0
                stdout = "+ diff\n"
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _fake_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="gefixt")))
        # Artefakt-Pflicht ist hier nicht der Prüfgegenstand — implement/fix
        # versprechen ohnehin kein Artefakt (siehe _ARTEFAKT_FELD_JE_STUFE),
        # nur die Review-Stufe (VERDIKT_DATEI) tut es in Tick 2.
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        task = _task(m.REVIEWING)

        # Tick 1: negatives Vor-Fix-Urteil liegt vor -> Fix-Stufe.
        ergebnis1 = pl.eine_stufe(task, tmp_path)
        assert ergebnis1 == "weiter"
        # Nachtrag 2c: kein Zustandswechsel — der Fix bleibt in REVIEWING.
        assert stubs["states"] == [], f"Fix hat den Zustand gewechselt: {stubs['states']}"
        # Die Fix-Stufe ist an ihrem Agent-Namen 'fix' erkennbar.
        assert stubs["agenten"][-1] == "fix"
        # Kritischer Fund 1: das Vor-Fix-Urteil muss jetzt weg sein.
        assert not (tmp_path / pl.stages.VERDIKT_DATEI).is_file()
        # task["state"] bleibt REVIEWING — kein Übergang zu übernehmen.

        # Tick 2: kein Verdikt mehr vorhanden -> _hat_negatives_verdikt ist
        # False -> die echte Review-Stufe muss laufen, nicht die Fix-Stufe.
        stubs["journal"].clear()
        ergebnis2 = pl.eine_stufe(task, tmp_path)

        # Der Agent-Name ist jetzt 'review', nicht mehr 'fix'.
        assert stubs["agenten"][-1] == "review"
        # Der stage_start-Journaleintrag muss 'review' nennen, nicht 'fix'.
        stage_start = next(e for e in stubs["journal"] if e[0][1] == "stage_start")
        meldung = stage_start[0][2]
        assert "'review'" in meldung
        assert "'fix'" not in meldung
        # Review ist die letzte Kettenstufe (next_state=GATING) — mit dem
        # gestubbten Artefakt-Check meldet die Kette entsprechend "fertig".
        assert ergebnis2 == "fertig"
        assert stubs["states"][-1] == (5, m.GATING)


class TestUnbekannterZustandDiagnose:
    def test_parkgrund_nennt_den_unbekannten_zustand(self, monkeypatch, stubs, tmp_path):
        pl.eine_stufe(_task(m.QUEUED), tmp_path)
        assert m.QUEUED in stubs["parks"][-1][1]


class TestUnerwarteteAusnahme:
    """Über den Brief hinaus: eine Ausnahme aus einem verdrahteten Modul darf
    den Task nicht aktiv und spurlos hängen lassen."""

    def test_ausnahme_in_set_state_wird_geloggt_und_parkt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="x")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        def _explodiert(*a, **kw):
            raise RuntimeError("DB weg")

        monkeypatch.setattr(pl.queue, "set_state", _explodiert)
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        assert any(e[0][1] == "stage_failed" and "DB weg" in str(e[0]) for e in stubs["journal"])
        assert stubs["parks"]


class TestCompareAndSwapVerweigert:
    """Selbstbeobachtung: queue.set_state ist ein Compare-and-Swap und kann
    False liefern (verbotener Übergang, ein anderer Schreiber war schneller),
    ohne zu werfen. Ohne diese Prüfung würde die Pipeline 'weiter'/'fertig'
    melden, obwohl der Task tatsächlich noch im alten Zustand feststeckt."""

    def test_gescheiterter_zustandswechsel_parkt_statt_weiter_zu_melden(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl.queue, "set_state", lambda tid, target, current: False)
        ergebnis = pl.eine_stufe(_task(m.PLANNING), tmp_path)
        assert ergebnis == "geparkt"
        assert stubs["parks"]


class TestPfadExtraktion:
    def test_pfad_wird_aus_letzter_zeile_der_antwort_gelesen(self, monkeypatch, stubs, tmp_path):
        # Der spec-Prompt erlaubt ausdrücklich 'Annahme:'-Zeilen vor dem
        # abschließenden Pfad — .text.strip() allein würde diese Zeilen mit
        # in den vermeintlichen Pfad ziehen.
        text = "Annahme: es gibt kein bestehendes Modul dafür.\ndocs/specs/y-design.md"
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=text)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ("spec_path", "docs/specs/y-design.md") in stubs["artefakte"]


class TestPfadNormalisierung:
    """Akzeptanzlauf 2026-08-14: der Spec-Agent lieferte einen Pfad in
    Markdown-Backticks; der Artefakt-Check suchte wortwörtlich danach
    (inklusive der Backticks) und parkte den Task, obwohl die Datei
    tatsächlich existierte. Jeder Test hier prüft eine Verpackungsform, die
    ein reales Modell trotz der Prompt-Anweisung 'antworte am Ende nur mit
    dem Pfad' hinzufügen kann."""

    ECHTER_PFAD = "docs/superpowers/specs/2026-08-14-ist-wochenende-design.md"

    def test_exakte_antwort_aus_dem_akzeptanzlauf(self, monkeypatch, stubs, tmp_path):
        # Die WÖRTLICHE Modellantwort aus dem Journal-Eintrag des Laufs, der
        # den Bug aufgedeckt hat — keine Nacherzählung, sondern das Original.
        text = f"`{self.ECHTER_PFAD}`"
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=text)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "weiter"
        assert ("spec_path", self.ECHTER_PFAD) in stubs["artefakte"]

    def test_backticks_werden_entfernt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="`docs/specs/x-design.md`")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        # Ohne die Bereinigung würde hier der Pfad MIT Backticks landen, und
        # _artefakt_vorhanden würde exakt diese (falsche) Zeichenkette prüfen.
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_umschliessender_codezaun_wird_entfernt(self, monkeypatch, stubs, tmp_path):
        text = "Hier ist die Spec:\n\n```\ndocs/specs/x-design.md\n```"
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=text)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        # Ohne Zaun-Erkennung wäre die 'letzte nicht-leere Zeile' die
        # schließende Zaun-Zeile '```', nicht der Pfad.
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_anfuehrungszeichen_werden_entfernt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text='"docs/specs/x-design.md"')))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_markdown_link_form_wird_aufgeloest(self, monkeypatch, stubs, tmp_path):
        text = "[Design-Spec](docs/specs/x-design.md)"
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=text)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        # Ohne Auflösung landete die komplette Link-Syntax im Feld statt
        # nur des Pfads dahinter.
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_umgebende_leerzeichen_und_abschliessender_punkt_werden_entfernt(self, monkeypatch, stubs, tmp_path):
        text = "   docs/specs/x-design.md.   "
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=text)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        # Ohne die Bereinigung bliebe entweder der Satzpunkt am Pfad kleben,
        # oder das umgebende Leerzeichen würde die Existenzprüfung verfehlen.
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_fuehrendes_punkt_slash_wird_entfernt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="./docs/specs/x-design.md")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ("spec_path", "docs/specs/x-design.md") in stubs["artefakte"]

    def test_absoluter_pfad_wird_abgelehnt(self, monkeypatch, stubs, tmp_path):
        # Ein absoluter Pfad lässt sich nicht sinnvoll unter dem Worktree
        # einordnen — das darf niemals als 'Artefakt fehlt' durchgehen,
        # sondern muss als eigene, diagnostizierbare Park-Ursache auffallen.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="/etc/passwd")))
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        grund = stubs["parks"][-1][1]
        assert "/etc/passwd" in grund
        assert stubs["artefakte"] == []

    def test_pfad_mit_elternverzeichnis_wird_abgelehnt(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=True, text="docs/../../../etc/passwd")))
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        grund = stubs["parks"][-1][1]
        assert "docs/../../../etc/passwd" in grund
        assert stubs["artefakte"] == []

    def test_park_grund_zeigt_den_rohen_modelltext_nicht_nur_den_bereinigten(self, monkeypatch, stubs, tmp_path):
        # Kern der Anforderung: wenn die Ablehnung selbst auf eine neue,
        # unvorhergesehene Formatierung trifft, ist der ROHE Text das Einzige,
        # was den Fund noch diagnostizierbar macht.
        roh = "  `/absolute/pfad.md`  "
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=roh)))
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        grund = stubs["parks"][-1][1]
        assert roh.strip() in grund


# ---------------------------------------------------------------------------
# Akzeptanzlauf 2026-08-15, Fund 2: implement/fix arbeiten testgetrieben über
# mehrere Dateien und laufen legitim länger als ein einzelner Dokument-Lauf —
# jede Stufe muss ihr eigenes Zeitbudget an runner.run durchreichen.
# ---------------------------------------------------------------------------


class TestFund2StufenspezifischesTimeout:
    def test_timeout_wird_je_stufe_durchgereicht(self, monkeypatch, stubs, tmp_path):
        aufgezeichnete_timeouts = []

        def _run(prompt, cwd, timeout=1800, agent=None, model=None):
            aufgezeichnete_timeouts.append(timeout)
            return RunResult(ok=True, text="x")

        def _gitctl_erfolg(*args, cwd=None, timeout=300):
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.backends, "hole", lambda name: _run)
        monkeypatch.setattr(pl.gitctl, "run", _gitctl_erfolg)  # review braucht _schreibe_diff
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        pl.eine_stufe(_task(m.SPECCING), tmp_path)
        pl.eine_stufe(_task(m.PLANNING), tmp_path)
        pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)
        pl.eine_stufe(_task(m.REVIEWING), tmp_path)  # kein Verdikt vorhanden -> echte review-Stufe

        _verdikt(tmp_path, "fail", [{"severity": "important", "what": "x"}])
        pl.eine_stufe(_task(m.REVIEWING), tmp_path)  # jetzt negatives Verdikt -> fix-Stufe

        erwartet = [
            pl.stages.fuer_state(m.SPECCING).timeout,
            pl.stages.fuer_state(m.PLANNING).timeout,
            pl.stages.fuer_state(m.IMPLEMENTING).timeout,
            pl.stages.fuer_state(m.REVIEWING).timeout,
            pl.stages.FIX_STAGE.timeout,
        ]
        assert aufgezeichnete_timeouts == erwartet
        # Der Kern der Anforderung: implement/fix bekommen NICHT dasselbe
        # Budget wie die drei Lese-Stufen.
        assert erwartet[2] == pl.stages.IMPLEMENT_TIMEOUT_SEKUNDEN
        assert erwartet[4] == pl.stages.IMPLEMENT_TIMEOUT_SEKUNDEN
        assert erwartet[0] == erwartet[1] == erwartet[3] == pl.stages.STANDARD_TIMEOUT_SEKUNDEN
        assert erwartet[2] != erwartet[0]

    def test_implement_und_fix_haben_ein_groesseres_budget_als_die_lese_stufen(self):
        assert pl.stages.IMPLEMENT_TIMEOUT_SEKUNDEN > pl.stages.STANDARD_TIMEOUT_SEKUNDEN


# ---------------------------------------------------------------------------
# Akzeptanzlauf 2026-08-15, Fund 1: ein Timeout allein darf bereits geleistete,
# bezahlte Arbeit nicht wegwerfen. implement/fix committen ihre Arbeit selbst,
# bevor sie (vom Modell aus gesehen) 'fertig' sind — läuft der Prozess danach
# über die Zeitgrenze, steht der Commit trotzdem schon auf dem Branch.
# ---------------------------------------------------------------------------


_ZEITUEBERSCHREITUNG = "Zeitüberschreitung nach 1800s"


class TestFund1ZeitueberschreitungLiefertTrotzdem:
    def test_implement_timeout_mit_neuem_commit_gilt_als_erfolg(self, monkeypatch, stubs, tmp_path):
        def _gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 0
                stdout = "1\n" if args[:1] == ("rev-list",) else ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error=_ZEITUEBERSCHREITUNG)))
        ergebnis = pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)

        assert ergebnis == "weiter"
        assert stubs["states"][-1] == (5, m.REVIEWING)
        assert stubs["parks"] == []
        stage_done = [e for e in stubs["journal"] if e[0][1] == "stage_done"]
        assert any("Zeitüberschreitung" in str(e) for e in stage_done), (
            "der Journal-Eintrag muss den Timeout-trotz-Erfolg-Fall erkennbar machen"
        )

    def test_implement_timeout_ohne_neuen_commit_parkt(self, monkeypatch, stubs, tmp_path):
        def _gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 0
                stdout = "0\n" if args[:1] == ("rev-list",) else ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error=_ZEITUEBERSCHREITUNG)))
        ergebnis = pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)

        assert ergebnis == "geparkt"
        assert stubs["parks"]
        assert stubs["states"] == []

    def test_spec_timeout_mit_bereits_bekanntem_artefakt_gilt_als_erfolg(self, monkeypatch, stubs, tmp_path):
        # Die Modellantwort ist bei einem Timeout leer (kein Text aus dem
        # abgebrochenen Prozess) — der übliche Pfad-Parsing-Weg hat also
        # nichts zu lesen. Ist der Pfad aus einem früheren Anlauf bereits am
        # Task hinterlegt UND existiert die Datei, zählt das als Produkt.
        pfad = "docs/superpowers/specs/2026-08-15-bekannt-design.md"
        ziel = tmp_path / pfad
        ziel.parent.mkdir(parents=True)
        ziel.write_text("# Spec")

        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error=_ZEITUEBERSCHREITUNG)))
        ergebnis = pl.eine_stufe(_task(m.SPECCING, spec_path=pfad), tmp_path)

        assert ergebnis == "weiter"
        assert stubs["states"][-1] == (5, m.PLANNING)
        assert stubs["parks"] == []

    def test_spec_timeout_findet_artefakt_ueber_verzeichnis_fallback(self, monkeypatch, stubs, tmp_path):
        # Kein bekannter Pfad am Task (Erstversuch) — der Fallback muss das
        # jüngste, seit Stufenstart geschriebene File im Artefaktverzeichnis
        # finden, wie in der Aufgabenstellung gefordert.
        fester_start = 1_800_000_000.0
        monkeypatch.setattr(pl.time, "time", lambda: fester_start)

        verzeichnis = tmp_path / pl.stages.SPEC_VERZEICHNIS
        verzeichnis.mkdir(parents=True)
        alt = verzeichnis / "alt-design.md"
        alt.write_text("# Alt")
        os.utime(alt, (fester_start - 100, fester_start - 100))
        neu = verzeichnis / "2026-08-15-neu-design.md"
        neu.write_text("# Neu")
        os.utime(neu, (fester_start + 5, fester_start + 5))

        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error=_ZEITUEBERSCHREITUNG)))
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)

        assert ergebnis == "weiter"
        assert ("spec_path", f"{pl.stages.SPEC_VERZEICHNIS}/2026-08-15-neu-design.md") in stubs["artefakte"]

    def test_zeitueberschreitung_ohne_produkt_erkennung_wuerde_hier_nicht_parken(self, monkeypatch, stubs,
                                                                                  tmp_path):
        # Gegenprobe zur Selbstprüfung: derselbe Timeout-Fehlertext, aber ohne
        # jedes Produkt (leeres Verzeichnis, kein Commit) — muss weiterhin
        # parken. Ohne diesen Test könnte die Erkennung 'jeder Timeout ist
        # ein Erfolg' geworden sein, ohne dass ein Test das auffängt.
        monkeypatch.setattr(pl.backends, "hole", lambda name:
                            _lauf(stubs, RunResult(ok=False, error=_ZEITUEBERSCHREITUNG)))
        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)
        assert ergebnis == "geparkt"
        assert stubs["parks"]
        assert "Zeitüberschreitung" in stubs["parks"][-1][1] or _ZEITUEBERSCHREITUNG in stubs["parks"][-1][1]


# ---------------------------------------------------------------------------
# Akzeptanzlauf 2026-08-15, Fund 3: das Gate und die Review-Stufe urteilen
# über `git diff main...HEAD` — ein unkommittetes Spec-/Plan-Dokument bleibt
# für beide unsichtbar, obwohl die Stufe ihre Aufgabe erfüllt hat.
# ---------------------------------------------------------------------------


class TestFund3ArtefaktWirdCommittet:
    def test_erfolgreiche_spec_stufe_committet_nur_ihr_artefakt(self, monkeypatch, stubs, tmp_path):
        pfad = "docs/superpowers/specs/2026-08-15-x-design.md"
        aufgezeichnete_aufrufe = []

        def _gitctl(*args, cwd=None, timeout=300):
            aufgezeichnete_aufrufe.append(args)

            class _R:
                # `diff --cached --quiet` antwortet 1 = "es ist etwas
                # gestaged" (siehe TestI2ArtefaktCommitIdempotent).
                returncode = 1 if args[0] == "diff" else 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=pfad)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)

        assert ergebnis == "weiter"
        assert aufgezeichnete_aufrufe == [
            ("add", "--", pfad),
            ("diff", "--cached", "--quiet", "--", pfad),
            ("commit", "-m", "docs(forge): Artefakt der Stufe 'spec' für Task 5 committet", "--", pfad),
        ], (
            "muss exakt diesen einen Pfad committen, nie 'git add -A' oder eine andere Datei"
        )

    def test_commit_wird_journalt(self, monkeypatch, stubs, tmp_path):
        pfad = "docs/superpowers/specs/2026-08-15-x-design.md"
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=pfad)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        pl.eine_stufe(_task(m.SPECCING), tmp_path)

        assert any(pfad in str(e) for e in stubs["journal"] if e[0][1] == "stage_done" and pfad in str(e[0][2]))

    def test_fehlgeschlagener_commit_parkt_den_task(self, monkeypatch, stubs, tmp_path):
        pfad = "docs/superpowers/specs/2026-08-15-x-design.md"

        def _kaputtes_gitctl(*args, cwd=None, timeout=300):
            class _R:
                returncode = 128
                stdout = ""
                stderr = "fatal: pathspec did not match any files"
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _kaputtes_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=pfad)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        ergebnis = pl.eine_stufe(_task(m.SPECCING), tmp_path)

        assert ergebnis == "geparkt"
        assert stubs["parks"]
        # Der Zustandswechsel darf nicht vollzogen worden sein — sonst gälte
        # eine Stufe als abgeschlossen, deren Artefakt gar nicht sichtbar ist.
        assert stubs["states"] == []

    def test_implement_bekommt_kein_dokument_artefakt(self, monkeypatch, stubs, tmp_path):
        # implement/fix versprechen kein Artefakt-Dokument (siehe
        # _ARTEFAKT_FELD_JE_STUFE) — der Dokument-Commit-Pfad
        # (_committe_artefakt, "docs(forge): Artefakt der Stufe ...") gilt
        # ausdrücklich nur für spec/plan. Ihre Arbeit wird stattdessen über
        # _committe_stufenarbeit committet, siehe TestC3StufenarbeitCommit.
        aufrufe = _gitctl_aufzeichnend(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        ergebnis = pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)

        assert ergebnis == "weiter"
        assert stubs["artefakte"] == []
        assert not any("Artefakt der Stufe" in str(a) for a in aufrufe)


class TestI2ArtefaktCommitIdempotent:
    """Abschluss-Review 2c, I2: `git commit -- pfad` endet mit 1 ("nothing to
    commit"), wenn das Artefakt schon so in HEAD steht — beim Requeue mit
    bestehender Spec oder nach einem Absturz zwischen Artefakt-Commit und
    set_state. Die Stufe hat ihre Aufgabe dann erfüllt; ein Park wäre falsch.
    Deshalb: `git add`, dann `git diff --cached --quiet -- pfad`; rc 0 heisst
    nichts gestaged, also nichts zu committen."""

    def _gitctl(self, monkeypatch, diff_rc):
        aufrufe = []

        def _run(*args, cwd=None, timeout=300):
            aufrufe.append(args)

            class _R:
                returncode = diff_rc if args[0] == "diff" else 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _run)
        return aufrufe

    def test_unveraendertes_artefakt_wird_nicht_committet(self, monkeypatch, tmp_path):
        aufrufe = self._gitctl(monkeypatch, diff_rc=0)
        assert pl._committe_artefakt(tmp_path, "spec", 5, "docs/x.md") is None
        assert [a[0] for a in aufrufe] == ["add", "diff"], aufrufe

    def test_gestagedes_artefakt_wird_committet(self, monkeypatch, tmp_path):
        aufrufe = self._gitctl(monkeypatch, diff_rc=1)
        assert pl._committe_artefakt(tmp_path, "spec", 5, "docs/x.md") is None
        assert [a[0] for a in aufrufe] == ["add", "diff", "commit"], aufrufe

    def test_spec_stufe_mit_unveraendertem_artefakt_geht_weiter(self, monkeypatch, stubs, tmp_path):
        pfad = "docs/superpowers/specs/2026-08-15-x-design.md"
        self._gitctl(monkeypatch, diff_rc=0)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text=pfad)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        assert pl.eine_stufe(_task(m.SPECCING), tmp_path) == "weiter"
        assert stubs["parks"] == []

    @pytest.fixture
    def repo(self, tmp_path):
        ort = tmp_path / "repo"
        ort.mkdir()

        def g(*args):
            return subprocess.run(["git", *args], cwd=str(ort), check=True,
                                  capture_output=True, text=True).stdout
        g("init", "-b", "main")
        g("config", "user.email", "test@example.com")
        g("config", "user.name", "Test")
        (ort / "README.md").write_text("hallo\n")
        g("add", "README.md")
        g("commit", "-m", "erster Commit")
        return ort, g

    def test_zweiter_commit_desselben_artefakts_ist_kein_fehler_gegen_echtes_git(self, monkeypatch, repo):
        ort, g = repo
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        (ort / "docs").mkdir()
        (ort / "docs" / "spec.md").write_text("# Spec\n")

        assert pl._committe_artefakt(ort, "spec", 5, "docs/spec.md") is None
        assert g("rev-list", "--count", "HEAD").strip() == "2"
        # Requeue / Absturz nach dem Commit: dieselbe Stufe committet erneut.
        assert pl._committe_artefakt(ort, "spec", 5, "docs/spec.md") is None
        assert g("rev-list", "--count", "HEAD").strip() == "2", "leerer Commit oder Fehler beim zweiten Mal"

    def test_neue_datei_wird_gegen_echtes_git_committet(self, monkeypatch, repo):
        # Gegenprobe zur Idempotenz: eine noch nicht getrackte Datei ist kein
        # "unverändert" — `git diff --quiet HEAD -- pfad` hätte sie mit rc 1
        # als Änderung gemeldet, `diff --cached` sieht sie nach dem add.
        ort, g = repo
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        (ort / "docs").mkdir()
        (ort / "docs" / "plan.md").write_text("# Plan\n")
        assert pl._committe_artefakt(ort, "plan", 5, "docs/plan.md") is None
        assert g("rev-list", "--count", "HEAD").strip() == "2"
        assert g("status", "--porcelain").strip() == ""


def _gitctl_aufzeichnend(monkeypatch, status_stdout="", returncodes=None):
    """Zeichnet alle gitctl-Aufrufe auf und beantwortet `git status` mit
    `status_stdout` (NUL-getrennt, wie `--porcelain -z` es liefert).
    `returncodes` kann je git-Unterbefehl einen abweichenden Rückgabewert
    setzen, z.B. {"commit": 1}."""
    aufrufe: list[tuple] = []
    codes = returncodes or {}

    def _gitctl(*args, cwd=None, timeout=300):
        aufrufe.append(args)

        class _R:
            returncode = codes.get(args[0] if args else "", 0)
            stdout = status_stdout if args and args[0] == "status" else ""
            stderr = "kaputt"
        return _R()

    monkeypatch.setattr(pl.gitctl, "run", _gitctl)
    return aufrufe


class TestC3StufenarbeitCommit:
    """Kritischer Fund C3 (Abschluss-Review 2026-09-09): implement/fix haben
    unter opencode `bash: deny` und damit kein git. Ihr Prompt verlangte
    trotzdem einen Commit, und die Pipeline committete nur Stufen mit
    Dokument-Artefakt. Ergebnis: die Arbeit blieb unkommittet, `git diff
    main...HEAD` war leer, das Review urteilte über nichts und das Gate sah
    'keine Änderungen im Branch'. Die Pipeline committet jetzt selbst."""

    def _lauf_implement(self, monkeypatch, stubs, tmp_path, status_stdout="", returncodes=None):
        aufrufe = _gitctl_aufzeichnend(monkeypatch, status_stdout, returncodes)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        return aufrufe, pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)

    def test_arbeit_wird_mit_expliziten_pfaden_committet(self, monkeypatch, stubs, tmp_path):
        aufrufe, ergebnis = self._lauf_implement(
            monkeypatch, stubs, tmp_path, "?? forge/neu.py\0 M forge/alt.py\0")
        assert ergebnis == "weiter"
        add = next(a for a in aufrufe if a[0] == "add")
        commit = next(a for a in aufrufe if a[0] == "commit")
        assert set(add[2:]) == {"forge/neu.py", "forge/alt.py"}
        assert set(commit[4:]) == {"forge/neu.py", "forge/alt.py"}
        # Niemals pauschal — der Worktree kann Fremdes enthalten.
        assert "-A" not in add and "--all" not in add

    def test_pipeline_interne_dateien_werden_nicht_mitcommittet(self, monkeypatch, stubs, tmp_path):
        # .forge/ ist Zwischenstand zwischen zwei Stufen. Ein mitcommittetes
        # altes review.json stünde im Diff, den das nächste Review beurteilt.
        aufrufe, _ = self._lauf_implement(
            monkeypatch, stubs, tmp_path,
            "?? .forge/review.json\0?? .forge/diff.patch\0?? forge/neu.py\0")
        add = next(a for a in aufrufe if a[0] == "add")
        assert list(add[2:]) == ["forge/neu.py"]

    def test_leerer_worktree_erzeugt_keinen_commit(self, monkeypatch, stubs, tmp_path):
        # Eine Stufe, die nichts hinterlassen hat, ist kein Pipeline-Fehler —
        # darüber urteilen Review und Gate, nicht der Commit.
        aufrufe, ergebnis = self._lauf_implement(monkeypatch, stubs, tmp_path, "")
        assert ergebnis == "weiter"
        assert not any(a[0] in ("add", "commit") for a in aufrufe)

    def test_fehlgeschlagener_commit_parkt(self, monkeypatch, stubs, tmp_path):
        _, ergebnis = self._lauf_implement(
            monkeypatch, stubs, tmp_path, "?? forge/neu.py\0", returncodes={"commit": 1})
        assert ergebnis == "geparkt"
        assert "Commit der Stufenarbeit" in stubs["parks"][-1][1]
        assert stubs["states"] == []

    def test_auch_die_fix_stufe_committet(self, monkeypatch, stubs, tmp_path):
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        aufrufe = _gitctl_aufzeichnend(monkeypatch, "?? forge/repariert.py\0")
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        assert pl.eine_stufe(_task(m.REVIEWING), tmp_path) == "weiter"
        commit = next(a for a in aufrufe if a[0] == "commit")
        assert "forge/repariert.py" in commit

    def test_umbenennung_nimmt_den_quellpfad_nur_beim_commit_mit(self, monkeypatch, stubs, tmp_path):
        # `--porcelain -z` hängt bei R/C den Quellpfad als eigenen Eintrag an.
        # Task 3, gemessen gegen echtes git: die Quelle existiert im
        # Arbeitsbaum nicht mehr und darf NICHT an `git add` (dort matcht sie
        # auf nichts, returncode 128, bricht das ganze add ab), muss aber an
        # `git commit`, sonst bleibt ihre Löschung uncommittet liegen.
        aufrufe, _ = self._lauf_implement(
            monkeypatch, stubs, tmp_path, "R  forge/neu.py\0forge/alt.py\0")
        add = next(a for a in aufrufe if a[0] == "add")
        commit = next(a for a in aufrufe if a[0] == "commit")
        assert set(add[2:]) == {"forge/neu.py"}
        assert set(commit[4:]) == {"forge/neu.py", "forge/alt.py"}

    def test_zeitueberschreitung_mit_unkommittierter_arbeit_gilt_als_geliefert(
            self, monkeypatch, stubs, tmp_path):
        # _timeout_produkt zählte nur Commits. Seit implement/fix gar nicht
        # mehr selbst committen können, wäre damit JEDE zeitüberschrittene
        # Arbeitsstufe leer — genau die bezahlte Arbeit, die dieser Zweig
        # retten soll, ginge verloren.
        aufrufe = _gitctl_aufzeichnend(monkeypatch, "?? forge/halbfertig.py\0")
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(
            stubs, RunResult(ok=False, error="Zeitüberschreitung nach 5400s")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path) == "weiter"
        assert stubs["parks"] == []
        commit = next(a for a in aufrufe if a[0] == "commit")
        assert "forge/halbfertig.py" in commit

    def test_zeitueberschreitung_ohne_jede_arbeit_parkt_weiterhin(self, monkeypatch, stubs, tmp_path):
        _gitctl_aufzeichnend(monkeypatch, "")
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(
            stubs, RunResult(ok=False, error="Zeitüberschreitung nach 5400s")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path) == "geparkt"


class TestC3GegenEchtesGit:
    """Dieselbe Naht wie oben, aber gegen ein echtes `git` statt gegen einen
    Stub. Genau diese Abdeckung fehlte: sämtliche Unit-Tests der Welle mockten
    subprocess, und drei Fehler, die zusammen keinen einzigen Task durchlaufen
    liessen, waren deshalb grün. Hier läuft `git status --porcelain -z` echt,
    und der Diff, den Review und Gate sehen, wird tatsächlich nachgesehen."""

    @pytest.fixture
    def repo(self, tmp_path):
        ort = tmp_path / "repo"
        ort.mkdir()

        def g(*args):
            subprocess.run(["git", *args], cwd=str(ort), check=True, capture_output=True)
        g("init", "-b", "main")
        g("config", "user.email", "test@example.com")
        g("config", "user.name", "Test")
        (ort / "README.md").write_text("hallo\n")
        g("add", "README.md")
        g("commit", "-m", "erster Commit")
        # Wie im Betrieb: der Task arbeitet auf einem eigenen Branch, und
        # `git diff main...HEAD` (die Sicht von Review und Gate) ist genau die
        # Differenz zu main.
        g("checkout", "-b", "forge/task-5")
        return ort

    def test_arbeit_landet_wirklich_im_diff_den_review_und_gate_sehen(
            self, monkeypatch, stubs, repo):
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        # Was eine implement-Stufe ohne Shell hinterlässt: Dateien im Worktree,
        # nichts committet. Dazu pipeline-interner Zwischenstand — bewusst
        # NICHT review.json/diff.patch (stages.VERDIKT_DATEI/DIFF_DATEI):
        # seit Fund F3 (Abschluss-Review Plan 2b) räumt genau ein
        # erfolgreicher Implement-Lauf diese beiden aus (siehe
        # TestStalesVerdiktNachImplement), ein generischer Dateiname prüft
        # hier unabhängig davon, dass .forge/ komplett vom Commit
        # ausgeschlossen bleibt.
        (repo / "forge").mkdir()
        (repo / "forge" / "neu.py").write_text("# neu\n")
        (repo / "README.md").write_text("hallo\nund tschuess\n")
        (repo / ".forge").mkdir()
        (repo / ".forge" / "zwischenstand.txt").write_text("pipeline-intern")

        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), repo) == "weiter"

        diff = subprocess.run(["git", "diff", "main...HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout
        assert "forge/neu.py" in diff
        assert "und tschuess" in diff
        # Der pipeline-interne Zwischenstand darf NICHT im Diff stehen.
        assert ".forge/zwischenstand.txt" not in diff
        assert (repo / ".forge" / "zwischenstand.txt").is_file()

    def test_geloeschte_datei_wird_mitcommittet(self, monkeypatch, stubs, repo):
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        (repo / "README.md").unlink()
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), repo) == "weiter"
        diff = subprocess.run(["git", "diff", "--name-status", "main...HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout
        assert diff.startswith("D\tREADME.md")

    def test_pfad_mit_leerzeichen_und_umlaut_ueberlebt(self, monkeypatch, stubs, repo):
        # `-z` liefert unquotierte Pfade. Ohne das käme hier git-eigene
        # C-Quotierung ("\303\244...") zurück und `git add` liefe ins Leere.
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        (repo / "ein Pfad mit Ümlaut.md").write_text("inhalt\n")
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), repo) == "weiter"
        # Auch hier `-z`: `git diff --name-only` quotiert den Pfad sonst auf
        # der Ausgabeseite genauso ("ein Pfad mit \303\234mlaut.md").
        dateien = subprocess.run(["git", "diff", "--name-only", "-z", "main...HEAD"], cwd=str(repo),
                                 capture_output=True, text=True).stdout.split("\0")
        assert "ein Pfad mit Ümlaut.md" in dateien

    def test_ohne_aenderung_kein_leerer_commit(self, monkeypatch, stubs, repo):
        monkeypatch.setattr(pl.gitctl, "run", _echtes_gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        assert pl.eine_stufe(_task(m.IMPLEMENTING), repo) == "weiter"
        anzahl = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(repo),
                                capture_output=True, text=True).stdout.strip()
        assert anzahl == "1"


class TestBackendAufruf:
    """Seit Task 8 kommt das Backend/Modell-Paar aus `ketten.waehle`, nicht
    mehr aus `stufe.backend`/`stufe.model` (Vorher-Zustand aus Plan 1). Die
    Werte hier sind bewusst fiktiv und tauchen weder in forge/stages.py noch
    in forge/ketten.py auf — nur so zeigt ein bestehender Test, dass die
    Pipeline wirklich die Kette befragt und nicht zufällig denselben Wert
    liefert, der auch in der Stage-Definition steht."""

    def test_spec_stufe_laeuft_mit_dem_glied_aus_der_kette(self, monkeypatch, stubs, tmp_path):
        """Die Pipeline darf runner.run nicht mehr fest verdrahtet aufrufen."""
        gesehen = {}

        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("fiktiv-backend-spec", "fiktiv/spec-modell-9000"))

        def _fake_hole(name):
            def _run(prompt, cwd, timeout, agent, model):
                gesehen["backend"] = name
                gesehen["agent"] = agent
                gesehen["model"] = model
                return RunResult(ok=True, text="egal")
            return _run

        monkeypatch.setattr(pl.backends, "hole", _fake_hole)
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)

        assert gesehen["backend"] == "fiktiv-backend-spec"
        assert gesehen["agent"] == "spec"
        assert gesehen["model"] == "fiktiv/spec-modell-9000"

    def test_review_stufe_laeuft_mit_dem_glied_aus_der_kette(self, monkeypatch, stubs, tmp_path):
        gesehen = {}

        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("fiktiv-backend-review", "fiktiv/review-modell-7000"))

        def _fake_hole(name):
            def _run(prompt, cwd, timeout, agent, model):
                gesehen["backend"] = name
                gesehen["model"] = model
                return RunResult(ok=True, text="egal")
            return _run

        monkeypatch.setattr(pl.backends, "hole", _fake_hole)
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

        assert gesehen["backend"] == "fiktiv-backend-review"
        assert gesehen["model"] == "fiktiv/review-modell-7000"


class TestGitStatusFehlschlag:
    def _kaputtes_gitctl(self, monkeypatch):
        class _Ergebnis:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Ergebnis())

    def test_pfade_melden_den_fehler_statt_leerer_liste(self, monkeypatch, tmp_path):
        self._kaputtes_gitctl(monkeypatch)
        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert zum_hinzufuegen == []
        assert zum_committen == []
        assert fehler is not None
        assert "128" in fehler

    def test_beide_listen_sind_ohne_umbenennung_gleich(self, monkeypatch, tmp_path):
        class _Zwei:
            returncode = 0
            stdout = " M a.py\0?? b.py\0"
            stderr = ""

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Zwei())
        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert fehler is None
        assert zum_hinzufuegen == zum_committen == ["a.py", "b.py"]

    def test_committe_gibt_den_fehler_weiter_statt_erfolg(self, monkeypatch, tmp_path):
        """Der stille C3-Fehlermodus: Erfolg melden, obwohl nichts committet ist."""
        self._kaputtes_gitctl(monkeypatch)
        fehler = pl._committe_stufenarbeit(tmp_path, "implement", 1)
        assert fehler is not None
        assert "status" in fehler.lower()

    def test_leerer_worktree_bleibt_ein_erfolg(self, monkeypatch, tmp_path):
        """Nichts zu committen ist kein Fehler — nur ein Fehlschlag ist einer."""
        class _Leer:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Leer())
        assert pl._committe_stufenarbeit(tmp_path, "implement", 1) is None

    def test_timeout_produkt_meldet_kein_produkt_bei_kaputtem_status(self, monkeypatch, tmp_path):
        """Signatur ist _timeout_produkt(stufe, task, worktree, start)."""
        self._kaputtes_gitctl(monkeypatch)
        monkeypatch.setattr(pl, "_hat_neuen_commit", lambda w: False)
        implement = next(s for s in pl.stages.ALLE_STUFEN if s.name == "implement")
        hat_produkt, pfad = pl._timeout_produkt(implement, {"id": 1}, tmp_path, 0.0)
        assert hat_produkt is False
        assert pfad is None


class TestGestagterRenameGegenEchtesGit:
    """Gemessen am 2026-09-10 gegen echtes git (siehe Task-Brief): der
    Quellpfad eines bereits im Index stehenden Renames matcht bei `git add`
    auf nichts mehr (returncode 128, bricht das ganze add ab), wird aber bei
    `git commit` gebraucht, sonst bleibt die Löschung der Quelle gestagt und
    uncommittet liegen. Deshalb zwei unterschiedlich befüllte Listen."""

    def _repo(self, tmp_path):
        def g(*args):
            return subprocess.run(["git", *args], cwd=tmp_path,
                                  capture_output=True, text=True)
        g("init", "-q")
        g("config", "user.email", "p@p")
        g("config", "user.name", "p")
        (tmp_path / "alt.py").write_text("inhalt\n")
        g("add", "-A")
        g("commit", "-qm", "start")
        return g

    def test_quelle_geht_an_commit_aber_nicht_an_add(self, tmp_path):
        """Gemessen am 2026-09-10: der Quellpfad eines gestagten Renames matcht
        bei `git add` auf nichts (returncode 128, bricht das ganze add ab),
        wird bei `git commit` aber gebraucht — sonst bleibt die Loeschung
        der Quelle gestagt und uncommittet liegen."""
        g = self._repo(tmp_path)
        g("mv", "alt.py", "neu.py")          # legt den Rename in den Index
        (tmp_path / "dazu.py").write_text("x\n")

        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert fehler is None
        assert "alt.py" not in zum_hinzufuegen, "Quelle gehoert nicht an git add"
        assert "alt.py" in zum_committen, "Quelle wird bei git commit gebraucht"
        assert "neu.py" in zum_hinzufuegen and "neu.py" in zum_committen
        assert "dazu.py" in zum_hinzufuegen and "dazu.py" in zum_committen

        # Der eigentliche Beweis: git add muss die Pfade annehmen.
        ergebnis = subprocess.run(["git", "add", "--", *zum_hinzufuegen], cwd=tmp_path,
                                  capture_output=True, text=True)
        assert ergebnis.returncode == 0, ergebnis.stderr

    def test_rename_landet_trotzdem_vollstaendig_im_commit(self, tmp_path):
        """Ohne den Quellpfad darf die Loeschung der Quelle nicht zurueckbleiben."""
        g = self._repo(tmp_path)
        g("mv", "alt.py", "neu.py")

        fehler = pl._committe_stufenarbeit(tmp_path, "implement", 1)
        assert fehler is None

        offen = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                               capture_output=True, text=True).stdout.strip()
        assert offen == "", f"nach dem Commit blieb offen: {offen!r}"
        vorhanden = subprocess.run(["git", "ls-files"], cwd=tmp_path,
                                   capture_output=True, text=True).stdout.split()
        assert "neu.py" in vorhanden
        assert "alt.py" not in vorhanden


class TestBudgetMeldung:
    # Modell der Spec-Stufe (SPECCING -> "spec" in forge/stages.py). Fest
    # verdrahtet statt aus stages.py nachgeschlagen, damit ein Refactor, der
    # stufe.model durch stufe.name oder ein Stage-/Chain-Objekt ersetzt,
    # hier eine falsche Zeichenkette produziert und der Test das bemerkt
    # (siehe Review-Finding 2: mit dem Namen statt Modell landet jeder
    # provider_von_modell()-Aufruf bei "antigravity" statt beim echten
    # Anbieter).
    SPEC_MODELL = "google/gemini-3.6-flash"

    def _budget_stub(self, monkeypatch):
        gebucht, erschoepft = [], []
        monkeypatch.setattr(pl.budget, "buche",
                            lambda model, tokens_in, tokens_out: gebucht.append((model, tokens_in, tokens_out)))
        monkeypatch.setattr(pl.budget, "markiere_erschoepft",
                            lambda model, grund: erschoepft.append((model, grund)))
        return gebucht, erschoepft

    def test_erfolgreicher_lauf_wird_gebucht(self, monkeypatch, stubs, tmp_path):
        gebucht, _ = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=True, text="egal", tokens_in=1200, tokens_out=80)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert gebucht and gebucht[-1] == (self.SPEC_MODELL, 1200, 80)

    def test_rate_limit_markiert_den_anbieter_als_erschoepft(self, monkeypatch, stubs, tmp_path):
        gebucht, erschoepft = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="rate limit", rate_limited=True)))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        # Auch ein rate-limiteter Lauf hat Kontingent gekostet und MUSS
        # gebucht werden — sonst zaehlt er nie gegen OBERGRENZEN, und
        # ist_erschoepft() bleibt fuer immer False, egal wie oft der
        # Anbieter ablehnt.
        assert gebucht and gebucht[-1][0] == self.SPEC_MODELL, \
            "Rate-limiteter Lauf wurde nicht gebucht"
        assert erschoepft and erschoepft[-1][0] == self.SPEC_MODELL, \
            "Rate-Limit wurde nicht (mit dem richtigen Modell) ans Budget gemeldet"

    def test_gewoehnlicher_fehler_erschoepft_nichts(self, monkeypatch, stubs, tmp_path):
        gebucht, erschoepft = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="irgendwas anderes")))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        # Auch ein gewoehnlicher (nicht rate-limiteter) Fehlschlag hat
        # Kontingent gekostet und muss gebucht werden — nur die
        # Erschoepfungs-Meldung bleibt aus.
        assert gebucht and gebucht[-1][0] == self.SPEC_MODELL, \
            "Gewoehnlicher Fehlschlag wurde nicht gebucht"
        assert erschoepft == []


class TestKettenwahl:
    def test_stufe_laeuft_mit_dem_glied_aus_der_kette(self, monkeypatch, stubs, tmp_path):
        gesehen = {}
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", "test/modell-x"))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: (
                gesehen.update(backend=name, model=model) or RunResult(ok=True, text="egal"))))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert gesehen["backend"] == "opencode"
        assert gesehen["model"] == "test/modell-x"

    def test_reviewer_kollision_parkt(self, monkeypatch, stubs, tmp_path):
        """Bleibt nur noch das Implementierer-Modell übrig, wird geparkt —
        Spec Zeile 183, "geparkt statt reviewt"."""
        # Ohne `verboten` liefert die Kette noch ein Glied: die Kette ist also
        # nicht trocken, es scheitert allein an der Reviewer-Regel.
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): None if verboten else ("agy", "irgendwas"))
        ergebnis = pl._eine_stufe_intern({"id": 1, "implement_model": "agy-modell"},
                                         1, m.REVIEWING, tmp_path)
        assert ergebnis == "geparkt"
        assert stubs["parks"], "kein Park-Eintrag geschrieben"
        # Review-Finding (Minor, 2026-09-09): "nicht leer" genügt nicht — ein
        # beliebiger anderer Park-Zweig (fehlendes Artefakt, Denial, ...)
        # hätte diese Zusicherung genauso erfüllt. Der Grund muss die Kette
        # tatsächlich nennen.
        assert "Kette" in stubs["parks"][-1][1], \
            f"Park-Grund nennt die erschöpfte Kette nicht: {stubs['parks'][-1][1]!r}"

    def test_erschoepfte_kette_parkt_nicht_und_verbraucht_den_task_nicht(
            self, monkeypatch, stubs, tmp_path):
        """Abschluss-Review C2 (2026-09-10): Kontingent-Erschöpfung ist kein
        Park-Grund. Spec Zeile 264: "Erst wenn jede Kette trocken ist, endet die
        Nacht" — der Task bleibt unangetastet liegen und läuft weiter, sobald
        das Kontingent zurück ist. Vorher parkte dieser Pfad, und weil der
        Daemon bei "geparkt" nicht schlief, war ein ganzer Backlog in Sekunden
        geparkt, mit je einem Worktree und Branch.

        Task 1 (2026-09-10): der Ausgang heißt seither "kontingent" statt
        "fehler" — sonst zählte der Daemon eine leere Kette in seine
        Fehler-Spirale und schrieb nach drei Ticks in Folge die
        Not-Aus-Datei."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "kontingent", "erschöpfte Kette darf nicht als Park gemeldet werden"
        assert stubs["parks"] == [], f"Task wurde trotzdem geparkt: {stubs['parks']}"
        assert stubs["states"] == [], f"Zustand wurde verändert: {stubs['states']}"
        assert stubs["fehlschlaege"] == [], "Fehlschlag-Zähler darf nicht anspringen"
        # Die Nacht endet still, wenn niemand sie protokolliert — der Grund muss
        # morgens im Journal stehen.
        assert any("erschöpft" in str(eintrag[0]) for eintrag in stubs["journal"]), \
            f"kein Journal-Eintrag zur erschöpften Kette: {stubs['journal']}"

    def test_erschoepfte_kette_ruft_kein_backend_auf(self, monkeypatch, stubs, tmp_path):
        """Kein Glied heißt kein Lauf — die Erschöpfung darf kein Kontingent kosten."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda *a, **kw: pytest.fail("Backend trotz erschöpfter Kette aufgerufen")))
        assert pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path) == "kontingent"

    def test_review_bekommt_das_implementierer_modell_als_verboten(self, monkeypatch, stubs, tmp_path):
        gesehen = {}

        def _waehle(name, verboten=frozenset()):
            gesehen[name] = verboten
            return ("agy", "claude-opus-4-6-thinking")

        monkeypatch.setattr(pl.ketten, "waehle", _waehle)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        pl._eine_stufe_intern({"id": 1, "implement_model": "nvidia/minimaxai/minimax-m3"},
                              1, m.REVIEWING, tmp_path)
        assert "nvidia/minimaxai/minimax-m3" in gesehen["review"]

    def test_implement_merkt_sich_sein_modell_am_task(self, monkeypatch, stubs, tmp_path):
        gemerkt = []
        monkeypatch.setattr(pl.queue, "merke_implement_modell",
                            lambda task_id, model: gemerkt.append((task_id, model)))
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", "test/impl"))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
        pl._eine_stufe_intern({"id": 1}, 1, m.IMPLEMENTING, tmp_path)
        assert gemerkt == [(1, "test/impl")]

    def test_fix_merkt_sich_sein_modell_am_task(self, monkeypatch, stubs, tmp_path):
        # Review-Finding 2 (2026-09-09): nur die implement-Hälfte von
        # `if stufe.name in ("implement", "fix")` war getestet. Eine
        # Verengung auf ausschließlich "implement" hätte diesen Test nicht
        # bemerkt — genau der Fehler, der implement_model nach einer
        # Fix-Runde stehen ließe und die Review-Stufe damit auf das gerade
        # fixende Modell laufen lassen könnte.
        _verdikt(tmp_path, "fail", [{"severity": "important", "what": "x"}])
        gemerkt = []
        monkeypatch.setattr(pl.queue, "merke_implement_modell",
                            lambda task_id, model: gemerkt.append((task_id, model)))
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", "test/fix"))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="gefixt")))
        pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)
        assert gemerkt == [(1, "test/fix")]

    def test_budget_bucht_das_kettenmodell_nicht_das_stufenmodell(self, monkeypatch, stubs, tmp_path):
        """Regression zum Budget-Pin (Review-Finding 1, 2026-09-09):
        `TestBudgetMeldung.SPEC_MODELL` pinnt zufällig denselben Wert, den
        auch der Default-Stub für `ketten.waehle` liefert — beide Pfade
        (stufe.model UND das Kettenmodell) stimmen für die spec-Stufe
        überein, sodass diese Zusicherung allein nicht beweist, welcher
        davon tatsächlich gebucht wird. Hier weicht das Kettenmodell bewusst
        von stufe.model ab: nur wenn budget.buche/markiere_erschoepft dem
        Kettenmodell folgen, kann dieser Test bestehen."""
        gebucht, erschoepft = [], []
        monkeypatch.setattr(pl.budget, "buche",
                            lambda model, tokens_in, tokens_out: gebucht.append(model))
        monkeypatch.setattr(pl.budget, "markiere_erschoepft",
                            lambda model, grund: erschoepft.append(model))
        kettenmodell = "test/kette-abweichend-vom-stufenmodell"
        stufenmodell = next(s.model for s in pl.stages.ALLE_STUFEN if s.name == "spec")
        assert kettenmodell != stufenmodell, "Testaufbau kaputt: Kette und Stufe stimmen überein"
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", kettenmodell))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="rate limit", rate_limited=True)))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert gebucht == [kettenmodell], \
            f"budget.buche bekam nicht das Kettenmodell: {gebucht}"
        assert erschoepft == [kettenmodell], \
            f"budget.markiere_erschoepft bekam nicht das Kettenmodell: {erschoepft}"

    def test_erschoepfte_kette_auf_fix_stufe_verbraucht_keine_fixrunde(self, monkeypatch, stubs, tmp_path):
        """Review-Finding 3 (2026-09-09): die Kettenwahl muss VOR der
        Fixrunden-Zählung stehen. Stünde sie danach, würde jeder Versuch gegen
        eine erschöpfte Kette eine Fixrunde verbrauchen, obwohl nie ein
        Fix-Lauf stattfand — nach MAX_FIXRUNDEN stünde der Task mit dem
        irreführenden Grund 'Fix-Runden-Grenze erreicht' geparkt, obwohl in
        Wirklichkeit kein Anbieter mehr verfügbar war.

        Seit Abschluss-Review C2 (2026-09-10) meldet dieser Pfad nicht mehr
        Park, seit Task 1 (2026-09-10) meldet er "kontingent" statt "fehler"
        (Kontingent ist kein Fehlverhalten) — die Zusicherung dieses Tests
        bleibt davon unberührt: gezählt wird nichts."""
        _verdikt(tmp_path, "fail", [{"severity": "important", "what": "x"}])
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)
        assert ergebnis == "kontingent"
        assert stubs["fixrunden"] == 0, \
            "eine erschöpfte Kette hat trotzdem eine Fixrunde verbraucht"
        assert stubs["parks"] == [], "Kontingent-Erschöpfung darf nicht parken"


class TestKontingentAusgang:
    def test_erschoepfte_kette_liefert_kontingent_nicht_fehler(self, monkeypatch, stubs, tmp_path):
        """'fehler' würde in die Fehler-Spirale zählen und die Forge abschalten."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "kontingent"

    def test_reviewer_kollision_bleibt_geparkt(self, monkeypatch, stubs, tmp_path):
        """Nur Erschöpfung ist Kontingent. Eine Kollision ist weiterhin ein Park."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: False)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "geparkt"

    def test_rate_limit_erschoepft_die_kette_liefert_kontingent(self, monkeypatch, stubs, tmp_path):
        """Fund F2 (Abschluss-Review Plan 2b): ein Rate-Limit, das gerade das
        letzte Kettenglied verbraucht (budget.markiere_erschoepft ist zu
        diesem Zeitpunkt bereits gelaufen), ist Kontingent und kein
        Fehlverhalten — 'fehler' würde sonst weiter in die Fehler-Spirale des
        Daemons zählen, obwohl kein Anbieter mehr da ist."""
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="rate limit", rate_limited=True)))
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "kontingent"
        assert stubs["states"] == [], "Zustand wurde trotz Kontingent veraendert"
        assert stubs["parks"] == [], "Kontingent-Erschöpfung darf nicht parken"

    def test_rate_limit_ohne_erschoepfte_kette_bleibt_fehler(self, monkeypatch, stubs, tmp_path):
        """Bleibt nach dem Rate-Limit noch ein Kettenglied übrig, ändert sich
        am bisherigen Verhalten nichts: 'fehler', Plan 3 hängt hier seine
        Wartezeit ein."""
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="rate limit", rate_limited=True)))
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: False)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "fehler"
        assert stubs["states"] == []
        assert stubs["parks"] == []


class TestFixStufeErreichbar:
    def _review_mit_verdikt(self, monkeypatch, tmp_path, ok: bool):
        """Lässt die Review-Stufe laufen und legt das angegebene Urteil ab.

        Angepasst gegenüber dem Brief-Wortlaut (siehe Report, Abschnitt
        "Angepasste Tests"): das Urteil wird HIER als Seiteneffekt des
        gestubbten Laufs geschrieben, nicht davor. In Produktion legt
        runner_agy.py review.json aus der Modellantwort an, WÄHREND der Lauf
        stattfindet — _waehle_stufe() sieht beim Eintritt in REVIEWING also
        noch KEIN Urteil und wählt zu Recht die echte Review-Stufe
        (ist_fix=False). Läge die Datei schon vor dem Aufruf da, würde
        _waehle_stufe() bei einem negativen Urteil sofort auf FIX_STAGE
        umschalten (ist_fix=True) — dann liefe gar nicht die Review-Stufe,
        sondern deren next_state-Sonderfall (ziel=IMPLEMENTING bei ist_fix),
        und der hier zu prüfende frühe Return (stufe.name == "review") käme
        nie zum Zug. Empirisch geprüft: mit der Vorab-Schreibweise aus dem
        Brief war test_negatives_verdikt_bleibt_in_reviewing schon VOR der
        Implementierung grün (RED-Lauf lieferte 3/3 passed statt der
        erwarteten 1 Fehlschlag) und wäre auch bei entferntem frühen Return
        grün geblieben.
        """
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)

        def _run(prompt, cwd, timeout, agent, model):
            (tmp_path / ".forge" / "review.json").write_text(
                json.dumps({"verdict": "pass" if ok else "fail",
                            "findings": [] if ok else [{"severity": "critical", "what": "x"}]}))
            return RunResult(ok=True, text="egal")

        monkeypatch.setattr(pl.backends, "hole", lambda name: _run)
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        return pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

    def test_negatives_verdikt_bleibt_in_reviewing(self, monkeypatch, stubs, tmp_path):
        """Sonst wählt _waehle_stufe nie die Fix-Stufe."""
        self._review_mit_verdikt(monkeypatch, tmp_path, ok=False)
        ziele = [z for z in stubs["states"]]
        assert m.GATING not in [z[1] for z in ziele], \
            "Review ist trotz negativem Urteil nach GATING vorgerueckt"

    def test_positives_verdikt_geht_weiter_nach_gating(self, monkeypatch, stubs, tmp_path):
        self._review_mit_verdikt(monkeypatch, tmp_path, ok=True)
        assert m.GATING in [z[1] for z in stubs["states"]]

    def test_naechster_durchlauf_waehlt_die_fix_stufe(self, tmp_path):
        """Der Beweis, dass die Schleife wirklich geschlossen ist."""
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text(
            json.dumps({"verdict": "fail",
                        "findings": [{"severity": "critical", "what": "x"}]}))
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is True
        assert stufe.name == "fix"


class TestStalesVerdiktNachImplement:
    """Fund F3 (Abschluss-Review Plan 2b): ein VERALTETES Urteil, das noch von
    vor dem letzten Absturz oder einem manuell wiedereingereihten Task im
    Worktree liegt, darf nicht überleben, bis der Task erneut REVIEWING
    erreicht — sonst schickt _waehle_stufe ihn in eine Fix-Runde, ohne dass
    der NEUE Code je reviewt wurde."""

    def test_implement_wirft_ein_vorliegendes_altes_verdikt_weg(self, monkeypatch, stubs, tmp_path):
        # Ein negatives Urteil liegt schon VOR dem Implement-Lauf da — genau
        # der Fall eines Absturzes zwischen set_state(-> IMPLEMENTING) und dem
        # bisherigen Aufräumen, oder eines manuell wiedereingereihten Tasks
        # mit stehendem Worktree.
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "veraltet"}])
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)

        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.IMPLEMENTING, tmp_path)

        assert ergebnis == "weiter"
        verdikt_datei = tmp_path / pl.stages.VERDIKT_DATEI
        assert not verdikt_datei.is_file(), \
            "veraltetes Review-Urteil hat den Implement-Lauf überlebt"
        # Ohne den Schnitt läse _waehle_stufe hier das alte "fail" und würde
        # sofort auf die Fix-Stufe umschalten, obwohl die Review-Stufe für den
        # neuen Code noch nie gelaufen ist.
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is False
        assert stufe.name == "review"


class TestFixBleibtInReviewing:
    """Nachtrag 2c: ein Fix wechselt keinen Zustand. Der nächste Tick trifft
    REVIEWING ohne Urteil an und lässt die echte Review-Stufe laufen."""

    def _fix_lauf(self, monkeypatch, stubs, tmp_path, ergebnis=None):
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: ergebnis or RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
        return pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

    def test_erfolgreicher_fix_wechselt_keinen_zustand(self, monkeypatch, stubs, tmp_path):
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert ergebnis == "weiter"
        assert stubs["states"] == [], f"Fix hat den Zustand gewechselt: {stubs['states']}"
        assert stubs["parks"] == []

    def test_nach_dem_fix_ist_das_urteil_weg_und_review_folgt(self, monkeypatch, stubs, tmp_path):
        self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert not (tmp_path / pl.stages.VERDIKT_DATEI).is_file()
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is False and stufe.name == "review"

    def test_erfolgreicher_fix_zaehlt_genau_eine_runde(self, monkeypatch, stubs, tmp_path):
        self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert stubs["fixrunden"] == 1

    def test_rate_limit_im_fix_verbraucht_keine_runde(self, monkeypatch, stubs, tmp_path):
        """Fund I4 aus dem 2b-Review: zwei Rate-Limits im Fix parkten den Task
        mit 'Fix-Runden-Grenze erreicht', ohne dass je ein Fix lief."""
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: False)
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path,
                                  RunResult(ok=False, rate_limited=True, error="429"))
        assert ergebnis == "fehler"
        assert stubs["fixrunden"] == 0
        assert (tmp_path / pl.stages.VERDIKT_DATEI).is_file(), "Urteil weg, obwohl kein Fix lief"

    def test_fehlgeschlagener_fix_verbraucht_keine_runde(self, monkeypatch, stubs, tmp_path):
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path,
                                  RunResult(ok=False, error="kaputt"))
        assert ergebnis == "geparkt"
        assert stubs["fixrunden"] == 0

    def test_grenze_wird_vor_dem_lauf_gelesen_nicht_gezaehlt(self, monkeypatch, stubs, tmp_path):
        stubs["fixrunden"] = pl.MAX_FIXRUNDEN
        gelaufen = []
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda *a, **k: gelaufen.append(1) or RunResult(ok=True, text="egal")))
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)
        assert ergebnis == "geparkt"
        assert gelaufen == [], "Fix lief trotz erreichter Grenze"
        assert stubs["fixrunden"] == pl.MAX_FIXRUNDEN, "Grenzprüfung hat gezählt"
        assert "Fix-Runden-Grenze" in stubs["parks"][-1][1]


class TestKontingentJournalKind:
    def test_erschoepfte_kette_journalt_als_kontingent(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        kinds = [a[1] for a, kw in stubs["journal"]]
        assert "kontingent" in kinds
        assert "stage_failed" not in kinds, "Kontingent zählt im Journal als Fehler"

    def test_rate_limit_das_die_kette_leert_journalt_als_kontingent(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda *a, **k: RunResult(ok=False, rate_limited=True, error="429")))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        kinds = [a[1] for a, kw in stubs["journal"]]
        assert "kontingent" in kinds
