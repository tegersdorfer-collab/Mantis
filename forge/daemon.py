"""Hauptschleife der Forge.

Plan 1 (Fundament): ein Task, ein Worktree, ein Claude-Lauf, Journal, parken.
Plan 2 (dieser Stand): der Trockenlauf ist der vollen fünfstufigen Pipeline
(forge/pipeline.py) und dem deterministischen Gate (forge/gate.py) gewichen.
`tick()` bringt einen Task pro Aufruf genau eine Stufe weiter; meldet die
Pipeline "fertig" (Zustand GATING erreicht), lässt der Daemon selbst das Gate
laufen. Ein grünes Gate bringt den Task nach `awaiting_restart_window`, wo er
liegen bleibt — Merge und Neustart-Etikette folgen erst in Plan 3. Gemerged
wird hier noch nichts.

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

from forge import gate, journal, pipeline, queue, worktree
from forge import models as m

log = logging.getLogger(__name__)

STOP_FILE = Path.home() / ".mantis-forge-stop"
MAX_CONSECUTIVE_FAILURES = 3
IDLE_SLEEP_SECONDS = 60
BLOCKED_SLEEP_SECONDS = 300
# Backoff nach einem Fehlschlag: ohne das folgen auf einen Absturz sofort
# weitere Ticks, ohne jede Bremse — bis zu drei Vollpreis-Claude-Läufe in
# Sekunden, bevor die Fehler-Spirale überhaupt greift.
FAILURE_SLEEP_SECONDS = 30
# Backoff nach einem geparkten Task. Ein Park ist kein Fortschritt, und er kann
# entstehen, ohne dass ein einziger LLM-Lauf stattfand (Reviewer-Kollision,
# fehlendes Artefakt, kaputter Worktree). Ohne Bremse zieht der nächste Tick
# sofort den nächsten Task, legt Worktree und Branch an, parkt ihn und macht
# weiter — ein ganzer Backlog ist so in Sekunden verbrannt. Kürzer als
# FAILURE_SLEEP_SECONDS, weil ein Park den Task aus der Queue nimmt und der
# nächste durchaus echte Arbeit sein kann.
PARK_SLEEP_SECONDS = 10
# Wartezeit, wenn alle Anbieter für diese Nacht leer sind. Deutlich länger als
# die anderen Bremsen: vor dem nächsten Kontingent-Fenster ändert sich nichts,
# und jeder Tick bis dahin ist eine Datenbankabfrage ohne Ergebnis.
KONTINGENT_SLEEP_SECONDS = 900


def should_run(failures: int) -> tuple[bool, str]:
    """Darf gerade gearbeitet werden? Zweiter Rückgabewert ist der Grund."""
    if STOP_FILE.exists():
        return False, f"Not-Aus aktiv ({STOP_FILE})"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return False, f"{failures} Fehlschläge in Folge — Daemon hält an"
    return True, "frei"


def _park(task_id: int, current: str, reason: str) -> None:
    """Parkt und journalt den Grund. Wie forge.pipeline._park: ein verworfener
    park()-Aufruf darf nicht spurlos bleiben, sonst hält ein Task, den nichts
    mehr bewegen kann, die Queue fest, ohne dass irgendwo sichtbar wird warum."""
    journal.log(task_id, "stage_failed", reason)
    if queue.zaehle_fehlschlag(task_id, current=current):
        # Schwelle bereits erreicht — queue.zaehle_fehlschlag hat den Task
        # automatisch geparkt. Ein zweiter park()-Aufruf mit dem spezifischeren
        # Grund würde nur noch am inzwischen falschen Ausgangszustand scheitern.
        return
    if not queue.park(task_id, current=current, reason=reason):
        journal.log(task_id, "stage_failed",
                     f"park() hat Task {task_id} nicht angenommen (Zustand '{current}')")


def _gate_und_abschliessen(task_id: int, baum: Path) -> str:
    """Der Task steht in GATING. Lässt das deterministische Gate laufen und
    schließt ihn ab — grün bringt ihn nach `awaiting_restart_window`, wo er
    liegen bleibt (Merge und Neustart-Etikette folgen erst in Plan 3), rot
    parkt ihn mit allen gesammelten Gründen.

    Rückgabe: 'fertig' bei grünem Gate, 'geparkt' bei rotem oder wenn der
    Zustandswechsel selbst scheitert (CAS verloren).
    """
    ergebnis = gate.pruefe(baum)
    if ergebnis.ok:
        journal.log(task_id, "gate_pass", "Gate bestanden — wartet auf Neustart-Fenster (Plan 3)")
        if not queue.set_state(task_id, m.AWAITING_RESTART, current=m.GATING):
            # CAS verloren — "fertig" zurückzugeben würde einen Fortschritt
            # vorgaukeln, der laut DB nie stattfand.
            _park(task_id, m.GATING,
                  f"Zustandswechsel {m.GATING} -> {m.AWAITING_RESTART} schlug fehl (Task {task_id})")
            return "geparkt"
        # Grünes Gate ist der Abschluss der Kette — der Fehlschlag-Zähler
        # beginnt neu (siehe forge/queue.py: versuche_zuruecksetzen).
        queue.versuche_zuruecksetzen(task_id)
        return "fertig"

    gruende = "; ".join(ergebnis.gruende)
    journal.log(task_id, "gate_fail", gruende)
    _park(task_id, m.GATING, f"Gate rot: {gruende}")
    return "geparkt"


def tick() -> str:
    """Ein Durchlauf. Rückgabe: 'leerlauf' | 'weiter' | 'fertig' | 'geparkt' | 'fehler' | 'kontingent'.

    Bringt den aktiven (oder nächsten) Task genau eine Pipeline-Stufe weiter
    (forge/pipeline.py). Meldet die Pipeline "fertig", ist der Task in GATING
    angekommen — dann läuft hier direkt im Anschluss das Gate. Ein Task, der
    bereits VOR diesem Tick in GATING steht (Wiederaufnahme nach einem
    Absturz zwischen "fertig" und dem Gate-Lauf), überspringt die Pipeline
    ganz: sie kennt den Zustand GATING nicht (deckt nur speccing..reviewing
    ab) und würde ihn sonst mit "Kein Stufen-Handler" parken, ohne dass das
    Gate je gelaufen wäre.

    Ab dem Moment, in dem `queue.claim_next()` einen Task liefert, steht dessen
    Zustand in der DB auf einer ACTIVE_STATES-Stufe. Alles danach läuft in
    einem try/except: jede Ausnahme wird geloggt (mit der echten Task-ID, nie
    None) und der Task wird bestmöglich geparkt, BEVOR die Ausnahme weiter
    nach oben gereicht wird — sonst bleibt er aktiv hängen, und der nächste
    Tick zieht genau denselben poisoned Task wieder, ohne Backoff.

    pipeline.eine_stufe() fängt ihre eigenen Ausnahmen bereits ab (nie mehr
    eine Exception aus einem Claude-Lauf) — das try/except hier bleibt trotzdem
    stehen, es sichert weiterhin echte Absturzquellen wie worktree.create()
    und einen unerwarteten Fehler im Gate-Anschluss selbst ab.
    """
    task = queue.claim_next()
    if task is None:
        return "leerlauf"

    task_id = task["id"]
    state = task["state"]
    try:
        baum = worktree.create(task_id)

        if state == m.GATING:
            return _gate_und_abschliessen(task_id, baum)

        ergebnis = pipeline.eine_stufe(task, baum)

        if ergebnis == "fertig":
            # eine_stufe() hat den Übergang bereits nach GATING vollzogen —
            # `state` muss das nachziehen, damit park() im Absturzfall (siehe
            # except unten) mit dem tatsächlichen DB-Zustand als CAS-Basis
            # arbeitet, nicht mit dem veralteten Ausgangszustand.
            state = m.GATING
            return _gate_und_abschliessen(task_id, baum)

        return ergebnis
    except Exception as exc:
        journal.log(task_id, "stage_failed", f"Tick-Absturz bei Task {task_id}: {exc}")
        try:
            # Buchführung für den Fehlschlag-Zähler — best effort, darf den
            # eigentlichen Park-Versuch gleich danach nicht verhindern.
            queue.zaehle_fehlschlag(task_id, current=state)
        except Exception:
            log.exception(f"Forge: Fehlschlag-Zählung für Task {task_id} selbst gescheitert")
        try:
            geparkt = queue.park(task_id, current=state, reason=f"Tick-Absturz: {exc}")
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
        elif ergebnis == "geparkt":
            time.sleep(PARK_SLEEP_SECONDS)
        elif ergebnis == "kontingent":
            time.sleep(KONTINGENT_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
