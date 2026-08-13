"""Unit-Tests für den git-Wrapper der Forge, insbesondere den Stale-Lock-Preflight.

Hintergrund: am 2026-07-25 blockierten drei Null-Byte-Locks das Mantis-Repo
19 Tage lang unbemerkt. Diese Tests halten die Erkennung fest."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
import subprocess
import time
from pathlib import Path

from forge import gitctl


def _repo_mit_lock(tmp_path, name="index.lock", alter_sekunden=0):
    """Baut ein Verzeichnis mit .git/<name> und setzt dessen Alter."""
    git_dir = tmp_path / ".git"
    (git_dir / "objects").mkdir(parents=True, exist_ok=True)
    lock = git_dir / name
    lock.write_text("")
    wann = time.time() - alter_sekunden
    os.utime(lock, (wann, wann))
    return tmp_path, lock


class TestStaleLocks:
    def test_alter_lock_ohne_git_prozess_ist_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=3600)
        assert gitctl.stale_locks(repo) == [lock]

    def test_frischer_lock_ist_nicht_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, _ = _repo_mit_lock(tmp_path, alter_sekunden=5)
        # Ein gerade angelegter Lock gehört vermutlich zu einer Operation,
        # die noch läuft — nur der Prozess ist noch nicht sichtbar.
        assert gitctl.stale_locks(repo) == []

    def test_laufender_git_prozess_macht_nichts_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: True)
        repo, _ = _repo_mit_lock(tmp_path, alter_sekunden=99999)
        assert gitctl.stale_locks(repo) == []

    def test_head_lock_wird_erkannt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, name="HEAD.lock", alter_sekunden=3600)
        assert gitctl.stale_locks(repo) == [lock]

    def test_maintenance_lock_wird_erkannt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        lock = git_dir / "maintenance.lock"
        lock.write_text("")
        wann = time.time() - 3600
        os.utime(lock, (wann, wann))
        assert gitctl.stale_locks(tmp_path) == [lock]

    def test_ohne_locks_leere_liste(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        assert gitctl.stale_locks(tmp_path) == []


class TestClearStaleLocks:
    def test_entfernt_und_meldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=3600)
        entfernt = gitctl.clear_stale_locks(repo)
        assert entfernt == [lock]
        assert not lock.exists()

    def test_laesst_frische_locks_liegen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=5)
        assert gitctl.clear_stale_locks(repo) == []
        assert lock.exists()

    def test_unlink_fehler_wird_nicht_hochgereicht(self, tmp_path, monkeypatch, caplog):
        # Wenn das Entfernen selbst fehlschlägt (z.B. Rechte, Race mit einem
        # gerade doch startenden git), darf clear_stale_locks NICHT crashen —
        # sonst reißt der Preflight den ganzen Forge-Loop mit.
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=3600)

        def kaputtes_unlink(self):
            raise OSError("Permission denied")

        monkeypatch.setattr(Path, "unlink", kaputtes_unlink)
        with caplog.at_level(logging.ERROR):
            entfernt = gitctl.clear_stale_locks(repo)
        assert entfernt == []
        assert any("Lock nicht entfernen" in r.message for r in caplog.records)

    def test_ein_kaputter_lock_haelt_andere_nicht_auf(self, tmp_path, monkeypatch, caplog):
        # Zwei stale Locks, einer lässt sich nicht entfernen — der andere muss
        # trotzdem verschwinden.
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, index_lock = _repo_mit_lock(tmp_path, name="index.lock", alter_sekunden=3600)
        head_lock = repo / ".git" / "HEAD.lock"
        head_lock.write_text("")
        wann = time.time() - 3600
        os.utime(head_lock, (wann, wann))

        original_unlink = Path.unlink

        def selektives_unlink(self):
            if self.name == "index.lock":
                raise OSError("Permission denied")
            return original_unlink(self)

        monkeypatch.setattr(Path, "unlink", selektives_unlink)
        with caplog.at_level(logging.ERROR):
            entfernt = gitctl.clear_stale_locks(repo)
        assert entfernt == [head_lock]
        assert index_lock.exists()
        assert not head_lock.exists()


class TestGitProcessRunning:
    def test_pgrep_fehlt_gilt_als_laufend(self, monkeypatch):
        # Sicherheitsrichtung: fehlt pgrep (FileNotFoundError, eine OSError),
        # wissen wir nichts — also lieber annehmen, dass ein git-Prozess läuft
        # und keinen Lock anfassen.
        def kaputtes_run(*args, **kwargs):
            raise FileNotFoundError("pgrep nicht gefunden")

        monkeypatch.setattr(subprocess, "run", kaputtes_run)
        assert gitctl.git_process_running() is True

    def test_pgrep_timeout_gilt_als_laufend(self, monkeypatch):
        def timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="pgrep", timeout=5)

        monkeypatch.setattr(subprocess, "run", timeout_run)
        assert gitctl.git_process_running() is True

    def test_pgrep_findet_prozess(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0),
        )
        assert gitctl.git_process_running() is True

    def test_pgrep_findet_keinen_prozess(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=1),
        )
        assert gitctl.git_process_running() is False


class TestSchwelle:
    def test_schwelle_ist_zehn_minuten(self):
        assert gitctl.STALE_AFTER_SECONDS == 600


class TestRun:
    def test_preflight_laeuft_vor_dem_git_aufruf(self, tmp_path, monkeypatch):
        # Der Preflight muss VOR dem eigentlichen git-Prozess laufen, nicht
        # danach — sonst räumt er einen Lock weg, den der gerade gestartete
        # Aufruf selbst gesetzt hat, oder merkt den alten Lock zu spät.
        reihenfolge = []

        def fake_clear(repo):
            reihenfolge.append("preflight")
            return []

        def fake_subprocess_run(cmd, **kwargs):
            reihenfolge.append("git")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gitctl, "clear_stale_locks", fake_clear)
        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

        gitctl.run("status", cwd=tmp_path)
        assert reihenfolge == ["preflight", "git"]

    def test_preflight_bekommt_das_richtige_repo(self, tmp_path, monkeypatch):
        gesehen = []
        monkeypatch.setattr(gitctl, "clear_stale_locks", lambda repo: gesehen.append(repo) or [])
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=""),
        )
        gitctl.run("status", cwd=tmp_path)
        assert gesehen == [tmp_path]

    def test_ruft_git_mit_den_uebergebenen_argumenten_auf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "clear_stale_locks", lambda repo: [])
        aufrufe = []

        def fake_subprocess_run(cmd, **kwargs):
            aufrufe.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
        gitctl.run("rev-parse", "--short", "HEAD", cwd=tmp_path)
        assert aufrufe == [["git", "rev-parse", "--short", "HEAD"]]

    def test_fehlgeschlagener_git_aufruf_wirft_nicht(self, tmp_path):
        # run() entscheidet nicht selbst über Erfolg/Misserfolg — das bleibt
        # beim Aufrufer (returncode/stderr). Bewusst OHNE subprocess-Mock: nur
        # ein echter Aufruf beweist, dass kein check=True hinzugekommen ist
        # (ein Mock würde das schlucken, egal was übergeben wird).
        ergebnis = gitctl.run("dieser-befehl-existiert-nicht", cwd=tmp_path)
        assert ergebnis.returncode != 0
