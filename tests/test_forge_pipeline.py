"""Die Stufenmaschine. Alle Außenkontakte gestubbt — geprüft wird die
Entscheidungslogik: wann geht es weiter, wann wird geparkt, wann in die
Fix-Runde."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from forge import models as m
from forge import pipeline as pl
from forge.runner import RunResult


@pytest.fixture
def stubs(monkeypatch):
    """Sammelt alle Seiteneffekte, statt sie auszuführen."""
    aufz = {"states": [], "parks": [], "journal": [], "artefakte": [], "agenten": [], "fixrunden": 0,
            "gitctl": []}

    monkeypatch.setattr(pl.queue, "set_state",
                        lambda tid, target, current: aufz["states"].append((tid, target)) or True)
    monkeypatch.setattr(pl.queue, "park",
                        lambda tid, current, reason: aufz["parks"].append((tid, reason)) or True)
    monkeypatch.setattr(pl.queue, "setze_artefakt",
                        lambda tid, feld, pfad: aufz["artefakte"].append((feld, pfad)))
    monkeypatch.setattr(pl.journal, "log",
                        lambda *a, **kw: aufz["journal"].append((a, kw)))

    def _fixrunde(tid):
        aufz["fixrunden"] += 1
        return aufz["fixrunden"]
    monkeypatch.setattr(pl.queue, "zaehle_fixrunde", _fixrunde)

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
        # Die Fix-Stufe hat Bash und kann sich selbst behelfen — nur die
        # eigentliche Review-Stufe ist darauf angewiesen, dass die Pipeline
        # vorbaut.
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])

        def _explodierendes_gitctl(*a, **kw):
            raise AssertionError("gitctl.run haette hier nicht aufgerufen werden duerfen")

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
    """Kritischer Fund 1, reproduziert über genau drei aufeinanderfolgende
    eine_stufe()-Aufrufe an EINEM Worktree:

    Tick 1: REVIEWING mit bereits vorliegendem negativem (Vor-Fix-)Urteil
            -> Fix-Stufe läuft, Task geht nach IMPLEMENTING.
    Tick 2: IMPLEMENTING läuft durch, Task geht zurück nach REVIEWING.
    Tick 3: Ohne den Fix in Task 6 fände _hat_negatives_verdikt dasselbe
            Vor-Fix-Urteil noch vor und schickte den Task sofort wieder in
            die Fix-Stufe, OHNE dass Review je gelaufen wäre. Mit dem Fix
            muss Tick 3 die echte Review-Stufe anfordern.
    """

    def test_dritter_tick_laeuft_review_stufe_nicht_fix_stufe(self, monkeypatch, stubs, tmp_path):
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
        # nur die Review-Stufe (VERDIKT_DATEI) tut es in Tick 3.
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)

        task = _task(m.REVIEWING)

        # Tick 1: negatives Vor-Fix-Urteil liegt vor -> Fix-Stufe.
        ergebnis1 = pl.eine_stufe(task, tmp_path)
        assert ergebnis1 == "weiter"
        assert stubs["states"][-1] == (5, m.IMPLEMENTING)
        # Die Fix-Stufe ist an ihrem Agent-Namen 'fix' erkennbar.
        assert stubs["agenten"][-1] == "fix"
        # Kritischer Fund 1: das Vor-Fix-Urteil muss jetzt weg sein.
        assert not (tmp_path / pl.stages.VERDIKT_DATEI).is_file()
        task["state"] = m.IMPLEMENTING

        # Tick 2: IMPLEMENTING läuft durch, Task geht zurück nach REVIEWING.
        ergebnis2 = pl.eine_stufe(task, tmp_path)
        assert ergebnis2 == "weiter"
        assert stubs["states"][-1] == (5, m.REVIEWING)
        task["state"] = m.REVIEWING

        # Tick 3: kein Verdikt mehr vorhanden -> _hat_negatives_verdikt ist
        # False -> die echte Review-Stufe muss laufen, nicht die Fix-Stufe.
        stubs["journal"].clear()
        ergebnis3 = pl.eine_stufe(task, tmp_path)

        # Der Agent-Name ist jetzt 'review', nicht mehr 'fix'.
        assert stubs["agenten"][-1] == "review"
        # Der stage_start-Journaleintrag muss 'review' nennen, nicht 'fix'.
        stage_start = next(e for e in stubs["journal"] if e[0][1] == "stage_start")
        meldung = stage_start[0][2]
        assert "'review'" in meldung
        assert "'fix'" not in meldung
        # Review ist die letzte Kettenstufe (next_state=GATING) — mit dem
        # gestubbten Artefakt-Check meldet die Kette entsprechend "fertig".
        assert ergebnis3 == "fertig"
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
                returncode = 0
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

    def test_implement_und_fix_committen_nichts_zusaetzliches(self, monkeypatch, stubs, tmp_path):
        # implement/fix versprechen kein Artefakt-Dokument (siehe
        # _ARTEFAKT_FELD_JE_STUFE) — der Commit-Schritt gilt ausdrücklich nur
        # für spec/plan, sonst würde hier ein bereits vom Agenten selbst
        # committeter Diff ein zweites Mal (und diesmal fälschlich pauschal)
        # committet.
        aufrufe = []

        def _gitctl(*args, cwd=None, timeout=300):
            aufrufe.append(args)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(pl.gitctl, "run", _gitctl)
        monkeypatch.setattr(pl.backends, "hole", lambda name: _lauf(stubs, RunResult(ok=True, text="fertig")))
        ergebnis = pl.eine_stufe(_task(m.IMPLEMENTING), tmp_path)

        assert ergebnis == "weiter"
        assert aufrufe == []


class TestBackendAufruf:
    def test_spec_stufe_laeuft_ueber_opencode(self, monkeypatch, stubs, tmp_path):
        """Die Pipeline darf runner.run nicht mehr fest verdrahtet aufrufen."""
        gesehen = {}

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

        assert gesehen["backend"] == "opencode"
        assert gesehen["agent"] == "spec"
        assert gesehen["model"] == "google/gemini-3.6-flash"

    def test_review_stufe_laeuft_ueber_agy(self, monkeypatch, stubs, tmp_path):
        gesehen = {}

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

        assert gesehen["backend"] == "agy"
        assert gesehen["model"] == "claude-opus-4-6-thinking"
