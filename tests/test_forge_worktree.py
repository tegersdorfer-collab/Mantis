"""Tests für die Worktree-Verwaltung — gegen ein echtes temporäres git-Repo.

Kein Netz, keine Mantis-DB: `git init` in tmp_path, ein Commit, fertig. git zu
stubben würde genau die Interaktion wegabstrahieren, die hier schiefgehen kann."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import subprocess

import pytest

from forge import worktree as wt


@pytest.fixture
def repo(tmp_path):
    """Ein minimales git-Repo mit einem Commit auf 'main'."""
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
    return ort


@pytest.fixture
def forge_root(tmp_path, monkeypatch):
    """Lenkt die Worktree-Wurzel in tmp_path, damit ~/Mantis-forge unberührt bleibt."""
    wurzel = tmp_path / "forge-worktrees"
    monkeypatch.setattr(wt, "FORGE_ROOT", wurzel)
    return wurzel


class TestNamen:
    def test_branch_name_ist_vorhersehbar(self):
        assert wt.branch_for(7) == "forge/task-7"

    def test_pfad_liegt_unter_der_wurzel(self, forge_root):
        assert wt.path_for(7) == forge_root / "task-7"

    def test_pfad_liegt_nicht_im_repo(self, forge_root):
        # Sonst würden Linter, Tests und Suchen die Worktrees mit einsammeln.
        from forge import MANTIS_REPO
        assert MANTIS_REPO not in wt.path_for(7).parents


class TestCreate:
    def test_legt_worktree_mit_branch_an(self, repo, forge_root):
        pfad = wt.create(3, base="main", repo=repo)
        assert pfad.is_dir()
        assert (pfad / "README.md").read_text() == "hallo\n"

    def test_branch_existiert_danach(self, repo, forge_root):
        wt.create(3, base="main", repo=repo)
        zweige = subprocess.run(
            ["git", "branch", "--list", "forge/task-3"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout
        assert "forge/task-3" in zweige

    def test_zweiter_aufruf_liefert_denselben_pfad(self, repo, forge_root):
        # Wiederaufsetzen nach Absturz darf nicht an einem bestehenden
        # Worktree scheitern.
        erster = wt.create(3, base="main", repo=repo)
        zweiter = wt.create(3, base="main", repo=repo)
        assert erster == zweiter
        assert zweiter.is_dir()

    def test_hauptcheckout_bleibt_auf_main(self, repo, forge_root):
        wt.create(3, base="main", repo=repo)
        aktuell = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert aktuell == "main"

    def test_beide_versuche_scheitern_wirft_runtimeerror(self, repo, forge_root):
        # Eine ungültige Basis lässt schon den ersten Versuch scheitern — und
        # weil dabei kein Branch entsteht, scheitert auch der Fallback-Versuch
        # ohne -b. Der Aufrufer muss das als RuntimeError mit gits stderr sehen,
        # statt einen kaputten Pfad zurückzubekommen.
        with pytest.raises(RuntimeError) as exc_info:
            wt.create(11, base="nicht-existente-basis", repo=repo)
        meldung = str(exc_info.value)
        assert "worktree add fehlgeschlagen" in meldung
        # gits eigene Fehlermeldung muss durchgereicht werden, nicht verschluckt.
        assert "nicht-existente-basis" in meldung or "invalid reference" in meldung
        # Kein halb angelegtes Verzeichnis darf zurückbleiben.
        assert not wt.path_for(11).exists()

    def test_fallback_greift_wenn_branch_schon_existiert(self, repo, forge_root):
        # Szenario: ein früherer Anlauf hat den Branch schon erzeugt, der
        # Worktree wurde aber (z.B. durch remove()) wieder entfernt. Der
        # erste Versuch ("-b") muss scheitern, weil der Branch schon da ist;
        # erst der zweite Versuch (ohne -b, checkt den bestehenden Branch aus)
        # darf greifen.
        zweig = wt.branch_for(12)
        subprocess.run(
            ["git", "branch", zweig, "main"], cwd=str(repo), check=True, capture_output=True,
        )
        pfad = wt.create(12, base="main", repo=repo)
        # Beweis, dass der Fallback tatsächlich einen brauchbaren Worktree
        # erzeugt hat, nicht nur einen geglaubten Pfad zurückgibt.
        assert pfad.is_dir()
        assert (pfad / "README.md").read_text() == "hallo\n"
        aktueller_zweig = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(pfad), capture_output=True, text=True,
        ).stdout.strip()
        assert aktueller_zweig == zweig


class TestCreatePartiellerWorktree:
    def test_verzeichnis_ohne_git_wird_als_unfertig_erkannt_und_neu_gebaut(self, repo, forge_root):
        # I5: "git worktree add" legt den Verzeichnisinhalt an, BEVOR es .git
        # schreibt. Ein Absturz mittendrin hinterlässt genau das: ein
        # Verzeichnis, das is_dir() ist, aber kein Worktree. Vor dem Fix hätte
        # create() das kommentarlos zurückgegeben — Claude liefe in einem
        # Nicht-Repo, und der Task könnte trotzdem als "erfolgreich" geparkt werden.
        halbfertig = wt.path_for(20)
        halbfertig.mkdir(parents=True)
        (halbfertig / "rest-von-einem-absturz.txt").write_text("Müll")

        pfad = wt.create(20, base="main", repo=repo)

        assert pfad == halbfertig
        assert (pfad / ".git").exists()
        assert (pfad / "README.md").read_text() == "hallo\n"
        assert not (pfad / "rest-von-einem-absturz.txt").exists()


class TestExists:
    def test_false_wenn_nichts_da(self, forge_root):
        assert wt.exists(99) is False

    def test_true_nach_create(self, repo, forge_root):
        wt.create(4, base="main", repo=repo)
        assert wt.exists(4) is True


class TestRemove:
    def test_entfernt_verzeichnis(self, repo, forge_root):
        pfad = wt.create(5, base="main", repo=repo)
        assert wt.remove(5, repo=repo) is True
        assert not pfad.exists()

    def test_remove_ohne_worktree_ist_harmlos(self, repo, forge_root):
        assert wt.remove(98, repo=repo) is False

    def test_branch_bleibt_nach_remove_erhalten(self, repo, forge_root):
        # Der Branch ist die Arbeit. Ein geparkter Task muss nachvollziehbar
        # bleiben, auch wenn der Worktree weg ist.
        wt.create(6, base="main", repo=repo)
        wt.remove(6, repo=repo)
        zweige = subprocess.run(
            ["git", "branch", "--list", "forge/task-6"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout
        assert "forge/task-6" in zweige

    def test_fallback_wenn_git_worktree_remove_scheitert(self, repo, forge_root):
        # "git worktree remove --force" verweigert bei einem GESPERRTEN
        # Worktree (git worktree lock) selbst mit --force — dafür braucht es
        # "-f -f". Genau das provoziert den in remove() vorgesehenen
        # Fallback: shutil.rmtree + git worktree prune.
        pfad = wt.create(13, base="main", repo=repo)
        subprocess.run(
            ["git", "worktree", "lock", str(pfad)], cwd=str(repo), check=True, capture_output=True,
        )
        ergebnis = wt.remove(13, repo=repo)
        # Der Rückgabewert muss die Realität widerspiegeln: Verzeichnis weg —
        # das ist der Vertrag, den remove() laut Docstring einhält. (git selbst
        # prunt gesperrte Einträge grundsätzlich nicht, auch nach rmtree nicht
        # — das ist gits eigenes Sicherheitsverhalten, keine Aussage über den
        # Rückgabewert von remove().)
        assert ergebnis is True
        assert not pfad.exists()
