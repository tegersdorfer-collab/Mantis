"""Freigabe: der Weg von AWAITING_APPROVAL nach MERGED — oder zurück zu Timo.

Gegen gemocktes git (Vertrag) und einmal gegen ein echtes tmp-Repo
(Integration), wie TestC3GegenEchtesGit in test_forge_pipeline.py.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import freigabe as f
from forge import models as m
from forge import worktree as wt

# Vor jedem Monkeypatch eingesammelt (wie _echtes_gitctl in
# test_forge_pipeline.py): die `welt`-Fixture patcht f.worktree.remove auf
# einen Rekorder, BEVOR der Testkörper läuft. Ein `wt.remove` im Testkörper
# selbst wäre zu diesem Zeitpunkt schon der Rekorder, nicht mehr die echte
# Implementierung — deshalb hier, auf Modulebene, vor jeder Fixture.
_echte_worktree_remove = wt.remove


class _Git:
    """Antwortet auf gitctl.run nach einem Drehbuch. Schlüssel ist das erste
    Argument (git-Unterkommando); der Wert ist (returncode, stdout), eine
    Liste davon (wird der Reihe nach verbraucht, der letzte Eintrag gilt
    weiter) oder eine Funktion args -> (returncode, stdout)."""

    def __init__(self, antworten: dict):
        self.antworten = antworten
        self.aufrufe: list[tuple] = []

    def __call__(self, *args, cwd=None, timeout=300):
        self.aufrufe.append((args, cwd))
        wert = self.antworten.get(args[0], (0, ""))
        if isinstance(wert, list):
            wert = wert.pop(0) if len(wert) > 1 else wert[0]
        if callable(wert):
            wert = wert(args)
        rc, out = wert
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="CONFLICT" if rc else "")


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """Task 7 in AWAITING_APPROVAL, alle Seiteneffekte aufgezeichnet. Der
    Worktree-Ordner existiert (Abschluss-Review 2c, I6: freigeben prüft das,
    bevor es git im Worktree aufruft)."""
    (tmp_path / "task-7").mkdir()
    aufz = {"states": [], "parks": [], "journal": [], "worktree_removed": []}
    monkeypatch.setattr(f.queue, "hole", lambda tid: {"id": tid, "state": m.AWAITING_APPROVAL, "title": "T"})
    monkeypatch.setattr(f.queue, "set_state",
                        lambda tid, target, current: aufz["states"].append((tid, target, current)) or True)
    monkeypatch.setattr(f.queue, "park",
                        lambda tid, current, reason: aufz["parks"].append((tid, reason)) or True)
    monkeypatch.setattr(f.journal, "log", lambda *a, **k: aufz["journal"].append(a))
    monkeypatch.setattr(f.worktree, "remove", lambda tid, repo=None: aufz["worktree_removed"].append(tid) or True)
    monkeypatch.setattr(f.worktree, "path_for", lambda tid: tmp_path / f"task-{tid}")
    return aufz


def _git(monkeypatch, antworten):
    git = _Git(antworten)
    monkeypatch.setattr(f.gitctl, "run", git)
    return git


class TestFreigebenVertrag:
    def test_sauberer_fall_merged_und_raeumt_auf(self, monkeypatch, welt, tmp_path):
        """merge-base == main-Spitze: kein Nachziehen nötig."""
        # rev-parse wird zweimal gefragt: Branchname von HEAD, dann die Spitze von main.
        git = _git(monkeypatch, {"status": (0, ""),
                                 "rev-parse": [(0, "main\n"), (0, "abc\n")],
                                 "merge-base": (0, "abc\n")})

        assert f.freigeben(7, repo=tmp_path) == "gemerged"
        assert welt["states"] == [(7, m.MERGED, m.AWAITING_APPROVAL)]
        assert welt["worktree_removed"] == [7]
        merges = [a for a, _ in git.aufrufe if a[0] == "merge"]
        assert merges == [("merge", "--no-ff", "--no-edit", "forge/task-7")]
        assert ("branch", "-d", "forge/task-7") in [a for a, _ in git.aufrufe]

    def test_schmutziges_main_lehnt_ab_ohne_zu_parken(self, monkeypatch, welt, tmp_path):
        _git(monkeypatch, {"status": (0, " M forge/x.py\n"), "rev-parse": (0, "main\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == [] and welt["parks"] == []

    def test_hauptrepo_nicht_auf_main_lehnt_ab(self, monkeypatch, welt, tmp_path):
        _git(monkeypatch, {"status": (0, ""), "rev-parse": (0, "forge/pipeline\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == []

    def test_main_bewegt_und_nachziehen_konfliktiert_parkt(self, monkeypatch, welt, tmp_path):
        git = _git(monkeypatch, {"status": (0, ""),
                                 "rev-parse": [(0, "main\n"), (0, "neu\n")],
                                 "merge-base": (0, "alt\n"),
                                 "merge": lambda args: (0, "") if "--abort" in args else (1, "")})

        assert f.freigeben(7, repo=tmp_path) == "konflikt"
        assert welt["parks"] and "main hat sich bewegt" in welt["parks"][0][1]
        assert welt["states"] == []
        merges = [(a, cwd) for a, cwd in git.aufrufe if a[0] == "merge"]
        # Nachziehen im Worktree, dann abgebrochen — nie ein Merge in main.
        assert merges[0] == (("merge", "--no-edit", "main"), tmp_path / "task-7")
        assert merges[1][0] == ("merge", "--abort")
        assert all(cwd != tmp_path for _, cwd in merges), "Merge in main trotz Konflikt"

    def test_falscher_zustand_lehnt_ab(self, monkeypatch, welt, tmp_path):
        monkeypatch.setattr(f.queue, "hole", lambda tid: {"id": tid, "state": m.PARKED})
        _git(monkeypatch, {})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"


class TestI6UntrackedUndFehlenderWorktree:
    """Abschluss-Review 2c, I6: `git status --porcelain` meldet auch
    untracked Dateien — in ~/Mantis liegt praktisch immer eine (Notiz,
    Scratch-Skript), und jede Freigabe wäre "abgelehnt". Untracked Dateien
    können einen Merge nicht kaputt machen, solange der Branch sie nicht
    anlegt (dann bricht git selbst ab). Und: ein fehlender Worktree (Ledger
    T5) darf keinen git-Aufruf darin auslösen."""

    def test_untracked_datei_in_main_verhindert_die_freigabe_nicht(self, monkeypatch, welt, tmp_path):
        def _status(args):
            # Nur mit --untracked-files=no ist der Baum "sauber".
            return (0, "") if "--untracked-files=no" in args else (0, "?? notiz.md\n")
        git = _git(monkeypatch, {"status": _status,
                                 "rev-parse": [(0, "main\n"), (0, "abc\n")],
                                 "merge-base": (0, "abc\n")})
        assert f.freigeben(7, repo=tmp_path) == "gemerged"
        status = next(a for a, _ in git.aufrufe if a[0] == "status")
        assert "--untracked-files=no" in status, status
        assert "--porcelain" in status

    def test_geaenderte_getrackte_datei_lehnt_weiterhin_ab(self, monkeypatch, welt, tmp_path):
        _git(monkeypatch, {"status": (0, " M forge/x.py\n"), "rev-parse": (0, "main\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == []

    def test_fehlender_worktree_lehnt_ab_ohne_git_zu_mutieren(self, monkeypatch, welt, tmp_path):
        (tmp_path / "task-7").rmdir()
        git = _git(monkeypatch, {"status": (0, ""),
                                 "rev-parse": [(0, "main\n"), (0, "neu\n")],
                                 "merge-base": (0, "alt\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == [] and welt["parks"] == []
        assert not any(a[0] == "merge" for a, _ in git.aufrufe), "git merge trotz fehlendem Worktree"
        assert not any(cwd == tmp_path / "task-7" for _, cwd in git.aufrufe)

    def test_ablehnungsgrund_steht_im_log(self, monkeypatch, welt, tmp_path, caplog):
        # Der String-Vertrag bleibt "abgelehnt"; das Warum geht per
        # log.warning ans CLI (das logging auf WARNING konfiguriert).
        _git(monkeypatch, {"status": (0, ""), "rev-parse": (0, "forge/pipeline\n")})
        with caplog.at_level("WARNING", logger="forge.freigabe"):
            assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert any("forge/pipeline" in r.getMessage() for r in caplog.records)


class TestAblehnenUndNeuEinreihen:
    def test_ablehnen_parkt_mit_grund(self, monkeypatch, welt):
        assert f.ablehnen(7, "zu gross") is True
        assert welt["parks"] == [(7, "Abgelehnt: zu gross")]

    def test_neu_einreihen_ruft_requeue(self, monkeypatch, welt):
        gerufen = []
        monkeypatch.setattr(f.queue, "requeue", lambda tid: gerufen.append(tid) or True)
        assert f.neu_einreihen(7) is True
        assert gerufen == [7]


def _sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


class TestFreigebenGegenEchtesGit:
    def test_merged_den_branch_wirklich(self, monkeypatch, welt, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _sh("git", "init", "-q", "-b", "main", cwd=repo)
        _sh("git", "config", "user.email", "t@t", cwd=repo)
        _sh("git", "config", "user.name", "t", cwd=repo)
        (repo / "a.txt").write_text("a\n")
        _sh("git", "add", ".", cwd=repo)
        _sh("git", "commit", "-q", "-m", "init", cwd=repo)
        baum = tmp_path / "task-7"
        _sh("git", "worktree", "add", "-b", "forge/task-7", str(baum), "main", cwd=repo)
        (baum / "docs.md").write_text("neu\n")
        _sh("git", "add", ".", cwd=baum)
        _sh("git", "commit", "-q", "-m", "task", cwd=baum)
        # worktree.remove echt laufen lassen, gegen dieses Repo (siehe
        # _echte_worktree_remove oben: die welt-Fixture hat wt.remove zu
        # diesem Zeitpunkt bereits auf einen Rekorder gepatcht).
        monkeypatch.setattr(f.worktree, "remove",
                            lambda tid, repo=None: _echte_worktree_remove(tid, repo=repo))
        monkeypatch.setattr(wt, "path_for", lambda tid: baum)

        assert f.freigeben(7, repo=repo) == "gemerged"
        assert (repo / "docs.md").read_text() == "neu\n"
        assert not baum.exists()
        zweige = _sh("git", "branch", "--list", "forge/task-7", cwd=repo).stdout.strip()
        assert zweige == ""
