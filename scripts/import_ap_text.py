#!/usr/bin/env python3
"""Importiert einen AlphaProgression-Text-Export in die Gym-Datenbank."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domains.gym.ap_text_import import import_workouts, parse_export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datei", type=Path, help="AlphaProgression-Text-Export")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="zeigt geplante Workouts und neue Übungen ohne Schreibzugriffe",
    )
    args = parser.parse_args(argv)
    text = args.datei.read_text(encoding="utf-8-sig")
    workouts = parse_export(text)
    report = import_workouts(workouts, dry_run=args.dry_run)
    mode = "Dry-run" if report.dry_run else "Import"
    print(
        f"{mode}: {report.workouts_created} neue Workouts, "
        f"{report.workouts_skipped} übersprungen, "
        f"{len(report.unclear_workouts)} unklar, "
        f"{report.exercises_created} neue Übungen, "
        f"{report.sets_created} Sätze."
    )
    for item in report.new_workouts:
        print(f"  Neu: {item.date.isoformat()} — {item.title}")
    for item in report.skipped_workouts:
        existing_title = item.existing_title or "(ohne Titel)"
        print(
            f"  Übersprungen: {item.date.isoformat()} — {item.title} "
            f"↔ {existing_title}"
        )
    for item in report.unclear_workouts:
        existing_title = item.existing_title or "(ohne Titel)"
        print(
            f"  Unklar: {item.date.isoformat()} — {item.title} "
            f"↔ {existing_title}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
