"""Timos Griff an die Forge, bis Plan 3 den Telegram-Bot bringt.

    python3.14 -m forge.cli status
    python3.14 -m forge.cli approve <id>
    python3.14 -m forge.cli reject <id> "<grund>"
    python3.14 -m forge.cli requeue <id>
    python3.14 -m forge.cli stop

Dünne Hülle: die Logik liegt in forge/freigabe.py und forge/bericht.py.
"""
import argparse
import logging
import subprocess
import sys

from core import db

from forge import bericht, daemon, freigabe


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge.cli", description="Forge-Freigabe und -Status")
    sub = p.add_subparsers(dest="befehl", required=True)
    sub.add_parser("status", help="Morgenbericht drucken")
    a = sub.add_parser("approve", help="Task mergen (aus awaiting_approval)")
    a.add_argument("task_id", type=int)
    r = sub.add_parser("reject", help="Task ablehnen und parken")
    r.add_argument("task_id", type=int)
    r.add_argument("grund")
    q = sub.add_parser("requeue", help="geparkten/gescheiterten Task neu einreihen")
    q.add_argument("task_id", type=int)
    sub.add_parser("stop", help="weicher Stop: laufende Stufe endet, dann der Daemon")
    return p


def _daemon_laeuft() -> bool:
    """Läuft gerade ein Forge-Daemon? Abschluss-Review 2c, I3.

    `pgrep -f` matcht auf die volle Befehlszeile (`python3.14 -m forge.daemon`).
    Eigene kleine Funktion, damit Tests sie patchen können: pgrep würde auch
    einen pytest-Prozess treffen, dessen argv "forge.daemon" enthält."""
    try:
        return subprocess.run(["pgrep", "-f", "forge.daemon"], capture_output=True).returncode == 0
    except OSError:
        return False


def _stop() -> int:
    """Weicher Stop. Ohne laufenden Daemon wird KEINE Halt-Datei geschrieben:
    daemon.main() räumt sie beim nächsten Start als veraltet weg (ein Halt
    ist eine Bitte an den laufenden Daemon), die Nacht liefe also trotz
    "Halt angefordert" — Abschluss-Review 2c, I3."""
    if not _daemon_laeuft():
        print("Kein Daemon läuft — eine Halt-Datei würde beim nächsten Start als veraltet entfernt. "
              "Für 'heute Nacht nicht': `touch ~/.mantis-forge-stop` (Not-Aus, von Hand entfernen) "
              "oder `launchctl bootout gui/$(id -u)/com.mantis.forge`.")
        return 1
    daemon.HALT_FILE.write_text("stop\n")
    print(f"Halt angefordert ({daemon.HALT_FILE}) — der Daemon beendet sich nach dem laufenden Tick.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Abschluss-Review 2c, I6: freigabe.freigeben() sagt seinen Ablehnungsgrund
    # nur per log.warning, der String-Vertrag ("abgelehnt") bleibt für Plan 3.
    # Ohne Handler hinge der Grund am logging.lastResort-Fallback; hier
    # explizit, damit "Task 7: abgelehnt" im Terminal verlässlich eine Zeile
    # mit dem Warum davor hat.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = _parser().parse_args(argv)
    if args.befehl == "stop":
        return _stop()

    db.init_pool()
    if args.befehl == "status":
        print(bericht.morgenbericht(), end="")
        return 0
    if args.befehl == "approve":
        ergebnis = freigabe.freigeben(args.task_id)
        print(f"Task {args.task_id}: {ergebnis}")
        return 0 if ergebnis == "gemerged" else 1
    if args.befehl == "reject":
        ok = freigabe.ablehnen(args.task_id, args.grund)
        print(f"Task {args.task_id}: {'geparkt' if ok else 'nicht in awaiting_approval'}")
        return 0 if ok else 1
    if args.befehl == "requeue":
        ok = freigabe.neu_einreihen(args.task_id)
        print(f"Task {args.task_id}: {'neu eingereiht' if ok else 'nicht geparkt/gescheitert'}")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
