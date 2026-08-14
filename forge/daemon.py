"""Hauptschleife der Forge.

Plan 1 (Fundament): ein Task, ein Worktree, ein Claude-Lauf, Journal, parken.
Gemerged wird noch nichts — die Pipeline kommt in Plan 2, Merge und
Neustart-Etikette in Plan 3.

Der Daemon hält sich an zwei Bremsen: Not-Aus-Datei und drei Fehlschläge in Folge.

Bewusst KEINE Bremse: eine parallel laufende interaktive Claude-Sitzung. Timo
hat sich am 2026-08-14 dagegen entschieden — die Forge soll durchlaufen, auch
während er selbst arbeitet. Der Preis: beide ziehen vom selben Kontingent, und
auf 16 GB RAM konkurriert die Test-Suite mit allem anderen. Ab Plan 3 ist die
Budget-Reserve die einzige Schranke dagegen; sie muss entsprechend
konservativ ausgelegt sein.
"""
import logging
import sys
import time
from pathlib import Path

from core import db

from forge import journal, queue, runner, worktree

log = logging.getLogger(__name__)

STOP_FILE = Path.home() / ".mantis-forge-stop"
MAX_CONSECUTIVE_FAILURES = 3
IDLE_SLEEP_SECONDS = 60
BLOCKED_SLEEP_SECONDS = 300
# Backoff nach einem Fehlschlag: ohne das folgen auf einen Absturz sofort
# weitere Ticks, ohne jede Bremse — bis zu drei Vollpreis-Claude-Läufe in
# Sekunden, bevor die Fehler-Spirale überhaupt greift.
FAILURE_SLEEP_SECONDS = 30


def should_run(failures: int) -> tuple[bool, str]:
    """Darf gerade gearbeitet werden? Zweiter Rückgabewert ist der Grund."""
    if STOP_FILE.exists():
        return False, f"Not-Aus aktiv ({STOP_FILE})"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return False, f"{failures} Fehlschläge in Folge — Daemon hält an"
    return True, "frei"


def _trockenlauf_prompt(task: dict) -> str:
    """Plan-1-Prompt: nur orientieren, nichts ändern."""
    return (
        "Du arbeitest in einem isolierten git-Worktree des Mantis-Projekts.\n"
        f"Anstehende Aufgabe: {task['title']}\n"
        f"{task.get('description') or ''}\n\n"
        "Das ist ein Trockenlauf. Ändere KEINE Dateien und committe nichts. "
        "Lies dich ein und antworte in höchstens 10 Zeilen: welche Dateien wären "
        "für diese Aufgabe relevant, und wo liegt die größte Unsicherheit?"
    )


def tick() -> str:
    """Ein Durchlauf. Rückgabe: 'leerlauf' | 'trockenlauf' | 'fehler'.

    Ab dem Moment, in dem `queue.claim_next()` einen Task liefert, steht dessen
    Zustand in der DB auf einer ACTIVE_STATES-Stufe. Alles danach läuft in
    einem try/except: jede Ausnahme wird geloggt (mit der echten Task-ID, nie
    None) und der Task wird bestmöglich geparkt, BEVOR die Ausnahme weiter
    nach oben gereicht wird — sonst bleibt er aktiv hängen, und der nächste
    Tick zieht genau denselben poisoned Task wieder, ohne Backoff.
    """
    task = queue.claim_next()
    if task is None:
        return "leerlauf"

    task_id = task["id"]
    try:
        journal.log(task_id, "stage_start", f"Trockenlauf für: {task['title']}")

        baum = worktree.create(task_id)
        ergebnis = runner.run(_trockenlauf_prompt(task), cwd=baum)

        journal.log(
            task_id,
            "stage_done" if ergebnis.ok else "stage_failed",
            (ergebnis.text or ergebnis.error or "")[:2000],
            tokens_in=ergebnis.tokens_in,
            tokens_out=ergebnis.tokens_out,
        )

        if not ergebnis.ok:
            geparkt = queue.park(task_id, current=task["state"],
                                  reason=f"Trockenlauf fehlgeschlagen: {ergebnis.error}")
            if not geparkt:
                journal.log(task_id, "stage_failed", "park() hat den fehlgeschlagenen Task nicht angenommen")
            return "fehler"

        geparkt = queue.park(task_id, current=task["state"],
                              reason="Plan-1-Trockenlauf abgeschlossen — Pipeline folgt in Plan 2")
        if not geparkt:
            # Ohne diese Prüfung würde ein verworfener park()-Aufruf als
            # "trockenlauf" durchgehen: failures würde auf 0 zurückgesetzt und
            # es gäbe keinen Backoff — eine ungebremste Schleife voller
            # Vollpreis-Läufe an einem Task, der in Wahrheit aktiv hängen blieb.
            journal.log(task_id, "stage_failed", "park() hat den erfolgreichen Task nicht angenommen")
            return "fehler"
        return "trockenlauf"
    except Exception as exc:
        journal.log(task_id, "stage_failed", f"Tick-Absturz bei Task {task_id}: {exc}")
        try:
            geparkt = queue.park(task_id, current=task["state"], reason=f"Tick-Absturz: {exc}")
            if not geparkt:
                log.error(f"Forge: Task {task_id} nach Absturz nicht parkbar — bleibt aktiv")
        except Exception:
            # Das Parken selbst darf die ursprüngliche Ausnahme nicht verdecken.
            log.exception(f"Forge: Parken nach Absturz für Task {task_id} selbst gescheitert")
        raise


def main() -> None:
    """launchd-Einstieg. Läuft bis zum Not-Aus oder bis zur Fehler-Spirale."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    db.init_pool()
    # Die Forge ist bewusst unabhängig vom laufenden Mantis-Assistant (siehe
    # forge/__init__.py) — sie darf sich also nicht darauf verlassen, dass
    # irgendein anderer Prozess vor ihr migriert hat.
    db.run_migrations()
    journal.log(None, "daemon_start", "Forge gestartet")
    log.info("Forge-Daemon gestartet")

    failures = 0
    while True:
        erlaubt, grund = should_run(failures)
        if not erlaubt:
            log.info(f"Forge pausiert: {grund}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # Die Bremse MUSS den launchd-Neustart überleben. Der Job läuft mit
                # KeepAlive=true; ein bloßes return würde 30s später neu starten, den
                # Zähler auf 0 setzen und dieselben drei Fehlläufe erneut verbrennen —
                # eine Endlosschleife statt einer Bremse. Die Not-Aus-Datei ist der
                # einzige Zustand, den ein Neustart nicht vergisst.
                STOP_FILE.write_text(f"Fehler-Spirale: {grund}\n")
                journal.log(None, "daemon_stop", f"{grund} — Not-Aus gesetzt, Freigabe durch Timo")
                return
            time.sleep(BLOCKED_SLEEP_SECONDS)
            continue

        try:
            ergebnis = tick()
        except Exception as exc:  # ein Task darf den Daemon nicht töten
            log.exception("Forge-Tick abgestürzt")
            journal.log(None, "stage_failed", f"Tick-Absturz: {exc}")
            ergebnis = "fehler"

        failures = failures + 1 if ergebnis == "fehler" else 0
        if ergebnis == "leerlauf":
            time.sleep(IDLE_SLEEP_SECONDS)
        elif ergebnis == "fehler":
            time.sleep(FAILURE_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
