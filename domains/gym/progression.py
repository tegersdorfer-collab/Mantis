"""Per-Übung Double-Progression und Trainings-PRs."""

from __future__ import annotations

import math


def _working_sets(rows: list[dict] | None) -> list[dict]:
    return sorted(
        (row for row in (rows or []) if not row.get("is_warmup")),
        key=lambda row: row.get("set_index") or 0,
    )


def _rep_bounds(metadata: dict | None) -> tuple[int, int, int]:
    metadata = metadata or {}
    exercise_type = str(metadata.get("exercise_type") or "compound").lower()
    default_min, default_max, default_start = (8, 12, 10) if exercise_type == "isolation" else (6, 10, 8)
    low = int(metadata.get("rep_min") or default_min)
    high = int(metadata.get("rep_max") or default_max)
    start = int(metadata.get("rep_start") or default_start)
    high = max(low, high)
    return low, high, min(high, max(low, start))


def _weight_step(metadata: dict | None) -> float | None:
    metadata = metadata or {}
    configured = metadata.get("weight_increment_kg")
    if configured is not None:
        try:
            step = float(configured)
        except (TypeError, ValueError):
            step = 0
        return step if step > 0 else None
    return {
        "barbell": 2.5,
        "dumbbell": 1.0,
        "machine": 2.5,
    }.get(str(metadata.get("equipment") or "").lower())


def progression_set_count(
    current_count: int,
    planned_count: int | None,
    previous_count: int = 0,
) -> int:
    """Hält mindestens aktuelle, letzte und geplante Satzanzahl bereit."""
    try:
        current_count = int(current_count)
    except (TypeError, ValueError):
        current_count = 0
    try:
        previous_count = int(previous_count)
    except (TypeError, ValueError):
        previous_count = 0
    try:
        planned_count = int(planned_count) if planned_count is not None else 0
    except (TypeError, ValueError):
        planned_count = 0
    planned_count = planned_count if 1 <= planned_count <= 20 else 3
    return max(current_count, previous_count, planned_count)


def recommend_next(
    current_sets: list[dict],
    previous_sets: list[dict] | None,
    metadata: dict | None = None,
    *,
    set_count: int | None = None,
) -> dict:
    """Berechnet Gewicht und Wiederholungsziel, ohne bei Einbruch zu entlasten."""
    current = _working_sets(current_sets)
    previous = _working_sets(previous_sets)
    if not current:
        return {"weight_kg": None, "target_reps": [], "incremented": False}

    low, high, start = _rep_bounds(metadata)
    count = max(len(current), int(set_count or 0))
    current_reps = [int(row.get("reps") or 0) for row in current]
    current_reps.extend([None] * (count - len(current_reps)))
    weight = current[0].get("weight_kg")
    target = [start] * count
    incremented = False

    if previous:
        previous_reps = [int(row.get("reps") or 0) for row in previous]
        previous_goals = [
            min(high, max(low, previous_reps[index] if index < len(previous_reps) else start))
            for index in range(count)
        ]
        if any(reps is None or reps < goal for reps, goal in zip(current_reps, previous_goals)):
            # Wiederhole je Satz die bisherige Bestleistung, auch wenn einzelne
            # Positionen heute schon darüber lagen.
            target = [
                min(high, max(goal, reps if reps is not None else goal))
                for goal, reps in zip(previous_goals, current_reps)
            ]
        elif all(reps is not None and reps >= high for reps in current_reps):
            step = _weight_step(metadata)
            if step and weight is not None:
                weight = round(float(weight) + step, 4)
                target = [low] * count
                incremented = True
            else:
                target = [high] * count
        else:
            target = [min(high, max(low, reps + 1)) for reps in current_reps if reps is not None]
            target.extend([start] * (count - len(target)))

    return {
        "weight_kg": float(weight) if weight is not None else None,
        "target_reps": target,
        "incremented": incremented,
    }


def estimate_1rm(weight_kg: float | None, reps: int | None) -> float | None:
    """Epley-Schätzung; Körpergewicht und ungültige Sätze ergeben keinen Wert."""
    try:
        weight = float(weight_kg)
        count = int(reps)
    except (TypeError, ValueError):
        return None
    if weight <= 0 or not 1 <= count <= 30:
        return None
    return weight * (1 + count / 30)


def personal_records(
    current_sets: list[dict],
    historical_sets: list[dict] | None,
    *,
    load_mode: str = "external",
) -> dict[str, bool]:
    """Prüft Gewicht, Wiederholungen bei gleicher Last und Epley-e1RM."""
    current = _working_sets(current_sets)
    history = _working_sets(historical_sets)
    weight_pr = reps_pr = e1rm_pr = False
    if not history:
        return {"weight": False, "reps": False, "e1rm": False}

    old_weights = [float(row["weight_kg"]) for row in history
                   if row.get("weight_kg") is not None and float(row["weight_kg"]) > 0]
    old_e1rms = [estimate_1rm(row.get("weight_kg"), row.get("reps")) for row in history]
    old_e1rms = [value for value in old_e1rms if value is not None]
    for row in current:
        weight_value = row.get("weight_kg")
        reps_value = int(row.get("reps") or 0)
        if weight_value is not None and float(weight_value) > 0:
            weight = float(weight_value)
            if old_weights and weight > max(old_weights):
                weight_pr = True
            comparable = [
                int(old["reps"]) for old in history
                if old.get("reps") is not None and old.get("weight_kg") is not None
                and math.isclose(float(old["weight_kg"]), weight, abs_tol=0.001)
            ]
            if (max(comparable) if comparable else 0) < reps_value:
                reps_pr = True
            if load_mode not in {"added", "bodyweight"}:
                estimate = estimate_1rm(weight, reps_value)
                if estimate is not None and old_e1rms and estimate > max(old_e1rms):
                    e1rm_pr = True
        elif load_mode in {"bodyweight", "added"}:
            comparable = [
                int(old["reps"]) for old in history
                if old.get("reps") is not None and old.get("weight_kg") is None
            ]
            if comparable and reps_value > max(comparable):
                reps_pr = True
    return {"weight": weight_pr, "reps": reps_pr, "e1rm": e1rm_pr}

