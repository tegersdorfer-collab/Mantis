#!/usr/bin/env python3
"""Read-only authenticated smoke check for the openGym snapshot API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="PostgreSQL URL to read (for example mantis_test); never printed",
    )
    return parser.parse_args()


def _validate_snapshot(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), dict):
        raise ValueError("GET /og/api/data did not return a state object")
    state = payload["state"]
    required_state = {
        "workouts", "routines", "dayPlan", "week", "bodyweight", "customEx", "unit", "restSec",
    }
    missing = required_state - state.keys()
    if missing:
        raise ValueError(f"state is missing required fields: {', '.join(sorted(missing))}")
    if not isinstance(payload.get("rev"), int):
        raise ValueError("snapshot revision is not an integer")
    for key in ("workouts", "routines", "bodyweight", "customEx"):
        if not isinstance(state[key], list):
            raise ValueError(f"state.{key} must be an array")
    if not isinstance(state["dayPlan"], dict) or not isinstance(state["week"], dict):
        raise ValueError("state.dayPlan and state.week must be objects")
    if state["unit"] != "kg" or not isinstance(state["restSec"], int):
        raise ValueError("state.unit or state.restSec has an invalid type")
    for routine in state["routines"]:
        if not isinstance(routine, dict) or not {"id", "name", "emoji", "ex"}.issubset(routine):
            raise ValueError("a routine is missing required openGym fields")
        for item in routine["ex"]:
            if not isinstance(item, dict) or not {"id", "sets", "reps"}.issubset(item):
                raise ValueError("a routine exercise is missing required openGym fields")
    for exercise in state["customEx"]:
        if not isinstance(exercise, dict) or not {
            "id", "n", "bp", "tg", "eq", "cat", "custom",
        }.issubset(exercise):
            raise ValueError("a custom exercise is missing required openGym fields")
    for measurement in state["bodyweight"]:
        if not isinstance(measurement, dict) or not {"d", "w"}.issubset(measurement):
            raise ValueError("a bodyweight entry is missing required openGym fields")
    for workout in state["workouts"]:
        if not isinstance(workout, dict) or not {
            "id", "d", "start", "end", "name", "entries", "vol",
        }.issubset(workout):
            raise ValueError("a workout is missing required openGym fields")
        if not isinstance(workout["entries"], list):
            raise ValueError("workout.entries must be an array")
        for entry in workout["entries"]:
            if not isinstance(entry, dict) or not {"id", "sets"}.issubset(entry):
                raise ValueError("a workout entry is missing required openGym fields")
            for item in entry["sets"]:
                if not isinstance(item, dict) or not {"w", "r", "done"}.issubset(item):
                    raise ValueError("a set is missing required openGym fields")
    return {
        "ok": True,
        "rev": payload["rev"],
        "workouts": len(state["workouts"]),
        "routines": len(state["routines"]),
        "exercises": len(state["customEx"]),
        "bodyweight_entries": len(state["bodyweight"]),
    }


def main() -> int:
    args = _arguments()
    os.environ["DATABASE_URL"] = args.database_url
    # The in-process login is synthetic; this script does not read or print a server token.
    os.environ["DASHBOARD_TOKEN"] = "gym-og-smoke-local-only"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import config
    from fastapi.testclient import TestClient
    from web.gym_app import create_gym_app

    config.DASHBOARD_TOKEN = os.environ["DASHBOARD_TOKEN"]
    with TestClient(create_gym_app()) as client:
        login = client.post("/auth/session", json={"token": config.DASHBOARD_TOKEN})
        if login.status_code != 200:
            raise RuntimeError(f"synthetic session could not be created: HTTP {login.status_code}")
        response = client.get("/og/api/data")
        if response.status_code != 200:
            raise RuntimeError(f"GET /og/api/data failed: HTTP {response.status_code}")
        result = _validate_snapshot(response.json())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"openGym smoke check failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
