#!/usr/bin/env python3
"""Seedet den Gym-Plan aus AP-Workouts; standardmäßig nur als Dry-Run."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domains.gym import ap_text_import, plans
from domains.gym.tracker import TrackerError


def _print_plan(data: dict, *, dry_run: bool) -> None:
    mode = "Dry-Run: keine Änderungen" if dry_run else "Plan wird gespeichert"
    print(f"{mode}\nPlan: {data['name']}\nStart: {data['start_date'].isoformat() if hasattr(data['start_date'], 'isoformat') else data['start_date']}")
    if data.get("start_reason"):
        print(f"Begründung: {data['start_reason']}")
    for day in data["days"]:
        exercises = day["exercises"]
        print(f"\n{day['name']} ({len(exercises)} Übungen)")
        for item in exercises:
            unit = "s" if item.get("target_unit") == "seconds" else "Wdh."
            span = (
                f"{item['rep_min']}" if item["rep_min"] == item["rep_max"]
                else f"{item['rep_min']}–{item['rep_max']}"
            )
            print(f"  {item['sort_order'] + 1}. {item['name']} · {item['target_sets']} Sätze · {span} {unit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Historie lesen und Plan anzeigen (Standard)")
    mode.add_argument("--apply", action="store_true", help="Plan atomar speichern und aktivieren")
    parser.add_argument("--name", default=plans.DEFAULT_PLAN_NAME, help="Planname aus den Workout-Titeln")
    parser.add_argument(
        "--ap-export", type=Path, metavar="DATEI",
        help="AlphaProgression-Textexport: Wdh.-Ziel je Übung aus dem jüngsten Workout desselben Plantags",
    )
    args = parser.parse_args(argv)

    try:
        ap_workouts = None
        if args.ap_export:
            ap_workouts = ap_text_import.parse_export(args.ap_export.expanduser().read_text(encoding="utf-8"))
        preview = plans.preview_seed(args.name, ap_workouts=ap_workouts)
        if args.apply:
            seeded = plans.seed_plan(args.name, ap_workouts=ap_workouts)
            _print_plan(preview, dry_run=False)
            print(f"\nGespeichert: Plan-ID {seeded['id']}")
        else:
            _print_plan(preview, dry_run=True)
    except TrackerError as error:
        print(error.message, file=sys.stderr)
        return 1
    except Exception as error:
        # Datenbankfehler dürfen keine Verbindungsparameter oder Secrets ausgeben.
        print(f"Seed fehlgeschlagen ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
