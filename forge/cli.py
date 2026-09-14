"""Timos Griff an die Forge, bis Plan 3 den Telegram-Bot bringt.

    python3.14 -m forge.cli status
    python3.14 -m forge.cli approve <id>
    python3.14 -m forge.cli reject <id> "<grund>"
    python3.14 -m forge.cli requeue <id>
    python3.14 -m forge.cli stop

Dünne Hülle: die Logik liegt in forge/freigabe.py und forge/bericht.py.
"""
import argparse
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.befehl == "stop":
        daemon.HALT_FILE.write_text("stop\n")
        print(f"Halt angefordert ({daemon.HALT_FILE}) — der Daemon beendet sich nach dem laufenden Tick.")
        return 0

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
