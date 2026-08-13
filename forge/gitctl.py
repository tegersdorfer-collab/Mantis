"""Jeder git-Aufruf der Forge läuft hier durch — mit Stale-Lock-Preflight davor.

Am 2026-07-25 starb im Mantis-Repo ein git-Prozess mitten in einer Operation und
hinterließ index.lock, HEAD.lock und objects/maintenance.lock (alle 0 Bytes).
Das Repo war danach 19 Tage schreibgeblockt, ohne dass es jemandem auffiel. Ein
Daemon, der so etwas nicht erkennt, hängt still — und still hängen ist die
schlechteste Betriebsart, die ein autonomer Loop haben kann.
"""
import logging
import os
import subprocess
import time
from pathlib import Path

from forge import MANTIS_REPO

log = logging.getLogger(__name__)

# Locks relativ zum git-Verzeichnis (nicht zwingend "<repo>/.git" — siehe _git_dir).
LOCK_PATHS = ("index.lock", "HEAD.lock", "objects/maintenance.lock")

# Jünger als das? Dann gehört der Lock vermutlich zu einer Operation, die gerade
# anläuft, auch wenn noch kein Prozess sichtbar ist. Lieber warten als kaputt machen.
STALE_AFTER_SECONDS = 600


def git_process_running() -> bool:
    """Läuft irgendwo auf dem Rechner ein git-Prozess?

    Bewusst grob: pgrep kann einen Prozess nicht einem Repo zuordnen. Ein
    falsches Ja bedeutet nur, dass wir einen Lock stehen lassen — das ist die
    sichere Richtung.
    """
    try:
        ergebnis = subprocess.run(["pgrep", "-x", "git"], capture_output=True, timeout=5)
        return ergebnis.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True     # im Zweifel annehmen, dass einer läuft


def _git_dir(repo: Path) -> Path:
    """Löst das tatsächliche git-Verzeichnis auf.

    Im Haupt-Checkout ist das `.git` als Verzeichnis. In einem linked worktree
    (Plan 2 committet dort) ist `.git` eine DATEI, die auf
    `<hauptrepo>/.git/worktrees/<name>` zeigt — Pfade, die blind `.git/` davor
    hängen, treffen dort nie einen echten Lock. `git rev-parse --git-dir` kennt
    den Unterschied; scheitert der Aufruf (kein Repo, kein git im PATH),
    fällt das Verhalten auf die alte Annahme `<repo>/.git` zurück.
    """
    try:
        ergebnis = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return Path(repo) / ".git"
    if ergebnis.returncode != 0:
        return Path(repo) / ".git"
    pfad = Path(ergebnis.stdout.strip())
    return pfad if pfad.is_absolute() else Path(repo) / pfad


def stale_locks(repo: Path, now: float | None = None) -> list[Path]:
    """Lock-Dateien, die niemandem mehr gehören."""
    if git_process_running():
        return []
    jetzt = now if now is not None else time.time()
    git_verzeichnis = _git_dir(repo)
    gefunden = []
    for rel in LOCK_PATHS:
        lock = git_verzeichnis / rel
        if lock.exists() and (jetzt - os.path.getmtime(lock)) > STALE_AFTER_SECONDS:
            gefunden.append(lock)
    return gefunden


def clear_stale_locks(repo: Path) -> list[Path]:
    """Entfernt verwaiste Locks und gibt zurück, welche es waren."""
    entfernt = []
    for lock in stale_locks(repo):
        try:
            lock.unlink()
            entfernt.append(lock)
            log.warning(f"Forge: verwaisten git-Lock entfernt: {lock}")
        except OSError as exc:
            log.error(f"Forge: konnte Lock nicht entfernen ({lock}): {exc}")
    return entfernt


def run(*args: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """Führt einen git-Befehl aus, nach Lock-Preflight. Wirft nicht — der
    Aufrufer entscheidet anhand von returncode und stderr.

    Zwei Stellen können sonst doch werfen, und beide werden hier synthetisch
    in ein CompletedProcess mit non-zero returncode umgewandelt, damit der
    Vertrag auch tatsächlich hält: eine Zeitüberschreitung des git-Prozesses
    selbst (subprocess.TimeoutExpired), und ein Lock, der zwischen `.exists()`
    und `os.path.getmtime()` im Preflight verschwindet (FileNotFoundError) —
    z.B. weil der git-Prozess, der ihn hielt, in genau diesem Moment fertig wird.
    """
    arbeitsverzeichnis = Path(cwd) if cwd is not None else MANTIS_REPO
    befehl = ["git", *args]

    try:
        clear_stale_locks(arbeitsverzeichnis)
    except FileNotFoundError as exc:
        fehler = f"Preflight: Lock verschwand während der Prüfung ({exc})"
        log.warning(f"Forge: {fehler}")
        return subprocess.CompletedProcess(args=befehl, returncode=1, stdout="", stderr=fehler)

    try:
        return subprocess.run(
            befehl,
            cwd=str(arbeitsverzeichnis),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        fehler = f"git-Aufruf abgebrochen nach {timeout}s: {exc}"
        log.warning(f"Forge: {fehler}")
        return subprocess.CompletedProcess(args=befehl, returncode=1, stdout="", stderr=fehler)
