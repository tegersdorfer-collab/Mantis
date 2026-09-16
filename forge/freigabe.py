"""Freigabe am Morgen: von AWAITING_APPROVAL nach MERGED — oder zurück zu Timo.

Die Logik liegt hier, nicht im CLI, damit Plan 3 sie aus dem Telegram-Bot
aufrufen kann, ohne sie zu kopieren. Kein Aufruf hier rät: ein schmutziges
main, ein Hauptrepo auf einem anderen Branch oder ein Konflikt beim Nachziehen
enden mit einer Meldung, nicht mit einem halben Merge.
"""
import logging
import subprocess
from pathlib import Path

from forge import MANTIS_REPO, daemon, gitctl, journal, queue, worktree
from forge import models as m
from forge.pipeline import BASIS_BRANCH

log = logging.getLogger(__name__)


def _ausgabe(ergebnis) -> str:
    return (ergebnis.stdout or "").strip()


def freigeben(task_id: int, repo: Path | None = None) -> str:
    """Merged den Branch des Tasks nach main.

    Rückgabe:
      "gemerged"  — MERGED, Worktree und Branch entfernt
      "konflikt"  — main hat sich bewegt und der Branch lässt sich nicht
                    konfliktfrei nachziehen, oder der Merge selbst scheitert;
                    Task ist PARKED mit Hinweis
      "abgelehnt" — nichts passiert: falscher Zustand, schmutziges main,
                    Hauptrepo nicht auf main, Worktree fehlt. Der Grund geht
                    per log.warning raus (forge.cli konfiguriert logging auf
                    WARNING, damit er im Terminal steht); der String-Vertrag
                    bleibt für Plan 3 unverändert.
    """
    task = queue.hole(task_id)
    if task is None or task["state"] != m.AWAITING_APPROVAL:
        log.warning(f"Forge-Freigabe: Task {task_id} steht nicht in {m.AWAITING_APPROVAL}")
        return "abgelehnt"

    quelle = Path(repo) if repo is not None else MANTIS_REPO
    zweig = worktree.branch_for(task_id)
    baum = worktree.path_for(task_id)

    # 0. Ohne Worktree kein Nachziehen und kein Aufräumen (Ledger T5): git
    #    in einem nicht existierenden Verzeichnis aufzurufen ist ein Fehler
    #    ohne Aussage. Timo entscheidet, ob der Branch von Hand gemerged wird.
    if not baum.is_dir():
        log.warning(f"Forge-Freigabe: Worktree {baum} fehlt — Branch {zweig} von Hand prüfen "
                    f"(git worktree remove --force, siehe forge/launchd/README.md)")
        return "abgelehnt"

    # 1. Das Hauptrepo muss auf main stehen und sauber sein. Wir wechseln
    #    keine Branches unter Timo weg.
    kopf = _ausgabe(gitctl.run("rev-parse", "--abbrev-ref", "HEAD", cwd=quelle))
    if kopf != BASIS_BRANCH:
        log.warning(f"Forge-Freigabe: Hauptrepo steht auf '{kopf}', nicht auf '{BASIS_BRANCH}'")
        return "abgelehnt"
    # Abschluss-Review 2c, I6: untracked Dateien zählen nicht als schmutzig.
    # In ~/Mantis liegt praktisch immer eine (Notiz, Scratch-Skript), und sie
    # können einen Merge nicht beschädigen — legt der Branch dieselbe Datei
    # an, bricht git den Merge selbst ab und wir landen bei "konflikt".
    status = gitctl.run("status", "--porcelain", "--untracked-files=no", cwd=quelle)
    if status.returncode != 0 or _ausgabe(status):
        log.warning("Forge-Freigabe: main ist nicht sauber — erst committen oder stashen: "
                    f"{_ausgabe(status)[:300] or 'git status fehlgeschlagen'}")
        return "abgelehnt"

    # 2. Hat main sich seit Anlage des Worktrees bewegt? Dann den Branch im
    #    Worktree nachziehen; ein Konflikt geht mit Hinweis an Timo zurück.
    basis = _ausgabe(gitctl.run("merge-base", BASIS_BRANCH, zweig, cwd=quelle))
    spitze = _ausgabe(gitctl.run("rev-parse", BASIS_BRANCH, cwd=quelle))
    if basis != spitze:
        nachziehen = gitctl.run("merge", "--no-edit", BASIS_BRANCH, cwd=baum)
        if nachziehen.returncode != 0:
            gitctl.run("merge", "--abort", cwd=baum)
            grund = (f"main hat sich bewegt und {zweig} lässt sich nicht konfliktfrei nachziehen: "
                     f"{(nachziehen.stderr or '').strip()[:300]}")
            queue.park(task_id, current=m.AWAITING_APPROVAL, reason=grund)
            journal.log(task_id, "parked", grund)
            return "konflikt"

    # 3. Der Merge selbst, mit Merge-Commit — im Log soll sichtbar bleiben,
    #    was die Forge als Einheit geliefert hat.
    merge = gitctl.run("merge", "--no-ff", "--no-edit", zweig, cwd=quelle)
    if merge.returncode != 0:
        gitctl.run("merge", "--abort", cwd=quelle)
        grund = f"Merge von {zweig} nach main fehlgeschlagen: {(merge.stderr or '').strip()[:300]}"
        queue.park(task_id, current=m.AWAITING_APPROVAL, reason=grund)
        journal.log(task_id, "parked", grund)
        return "konflikt"

    if not queue.set_state(task_id, m.MERGED, current=m.AWAITING_APPROVAL):
        # Gemerged ist gemerged — den Zustand nicht zurückdrehen, aber laut sein.
        log.error(f"Forge-Freigabe: Task {task_id} ist gemerged, aber der Zustandswechsel schlug fehl")
    journal.log(task_id, "merged", f"{zweig} nach {BASIS_BRANCH} gemerged (Freigabe)")
    worktree.remove(task_id, repo=quelle)
    gitctl.run("branch", "-d", zweig, cwd=quelle)
    return "gemerged"


def ablehnen(task_id: int, grund: str) -> bool:
    """AWAITING_APPROVAL -> PARKED mit Timos Grund."""
    ok = queue.park(task_id, current=m.AWAITING_APPROVAL, reason=f"Abgelehnt: {grund}")
    if ok:
        journal.log(task_id, "parked", f"Abgelehnt: {grund}")
    return ok


def neu_einreihen(task_id: int) -> bool:
    """PARKED/FAILED -> QUEUED, Zähler zurück (siehe queue.requeue)."""
    ok = queue.requeue(task_id)
    if ok:
        journal.log(task_id, "resumed", "Neu eingereiht (forge.cli requeue)")
    return ok


def daemon_laeuft() -> bool:
    """Läuft gerade ein Forge-Daemon? Abschluss-Review 2c, I3.

    `pgrep -f` matcht auf die volle Befehlszeile. Das Muster ist bewusst
    `-m forge\\.daemon` (Handoff 16.09., Befund 4): `forge.daemon` allein traf
    auch `forge.daemon_xyz` und jeden Prozess, der den Pfad im Argument hat.
    Eigene kleine Funktion, damit Tests sie patchen können — pgrep würde
    auch einen pytest-Prozess treffen, dessen argv das Muster enthält."""
    try:
        return subprocess.run(
            ["pgrep", "-f", r"-m forge\.daemon"], capture_output=True,
        ).returncode == 0
    except OSError:
        return False


def stoppen() -> str:
    """Weicher Stop, geteilt von forge.cli und forge.bot (Nachtrag 3a).

    Ohne laufenden Daemon wird KEINE Halt-Datei geschrieben: daemon.main()
    räumt sie beim nächsten Start als veraltet weg (ein Halt ist eine Bitte
    an den laufenden Daemon), die Nacht liefe also trotz "Halt angefordert"
    — Abschluss-Review 2c, I3. Rückgabe ist der Text für Terminal oder
    Telegram."""
    if not daemon_laeuft():
        return ("Kein Daemon läuft — eine Halt-Datei würde beim nächsten Start als veraltet entfernt. "
                "Für 'heute Nacht nicht': `touch ~/.mantis-forge-stop` (Not-Aus, von Hand entfernen) "
                "oder `launchctl bootout gui/$(id -u)/com.mantis.forge`.")
    daemon.HALT_FILE.write_text("stop\n")
    return f"Halt angefordert ({daemon.HALT_FILE}) — der Daemon beendet sich nach dem laufenden Tick."
