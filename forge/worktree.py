"""Isolierte Arbeitskopien pro Task.

Im Haupt-Checkout (~/Mantis) wird nie gearbeitet — dort läuft der produktive
Mantis. Jeder Task bekommt einen eigenen Worktree unter ~/Mantis-forge/task-<id>
mit eigenem Branch. Der Branch überlebt das Entfernen des Worktrees, damit die
Arbeit eines geparkten Tasks nachvollziehbar bleibt.
"""
import logging
import shutil
from pathlib import Path

from forge import FORGE_ROOT, MANTIS_REPO
from forge import gitctl

log = logging.getLogger(__name__)


def branch_for(task_id: int) -> str:
    return f"forge/task-{task_id}"


def path_for(task_id: int) -> Path:
    return FORGE_ROOT / f"task-{task_id}"


def exists(task_id: int) -> bool:
    return path_for(task_id).is_dir()


def create(task_id: int, base: str = "main", repo: Path | None = None) -> Path:
    """Legt den Worktree an — oder gibt den bestehenden zurück.

    Idempotent, weil der Daemon nach einem Absturz auf derselben Stufe wieder
    aufsetzt und dann auf einen bereits vorhandenen Worktree trifft.
    """
    ziel = path_for(task_id)
    if ziel.is_dir():
        return ziel

    ziel.parent.mkdir(parents=True, exist_ok=True)
    zweig = branch_for(task_id)
    quelle = Path(repo) if repo is not None else MANTIS_REPO

    ergebnis = gitctl.run(
        "worktree", "add", "-b", zweig, str(ziel), base, cwd=quelle,
    )
    if ergebnis.returncode != 0:
        # Zweiter Versuch ohne -b: der Branch kann aus einem früheren Anlauf
        # noch existieren, während der Worktree schon entfernt wurde.
        ergebnis = gitctl.run("worktree", "add", str(ziel), zweig, cwd=quelle)
    if ergebnis.returncode != 0:
        raise RuntimeError(f"worktree add fehlgeschlagen: {ergebnis.stderr.strip()}")

    log.info(f"Forge: Worktree für Task {task_id} unter {ziel}")
    return ziel


def remove(task_id: int, repo: Path | None = None) -> bool:
    """Entfernt den Worktree. Der Branch bleibt erhalten."""
    ziel = path_for(task_id)
    if not ziel.exists():
        return False

    quelle = Path(repo) if repo is not None else MANTIS_REPO
    ergebnis = gitctl.run("worktree", "remove", "--force", str(ziel), cwd=quelle)
    if ergebnis.returncode != 0:
        # git weigert sich gelegentlich (z.B. bei fremden Dateien im Baum).
        # Dann von Hand wegräumen und gits Registrierung nachziehen.
        shutil.rmtree(ziel, ignore_errors=True)
        gitctl.run("worktree", "prune", cwd=quelle)
    return not ziel.exists()
