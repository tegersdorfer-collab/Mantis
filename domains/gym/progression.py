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
    """Verwendet bei Historie die Satzanzahl der letzten Einheit."""
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
    if 1 <= previous_count <= 20:
        return previous_count
    if 1 <= current_count <= 20:
        return current_count
    if 1 <= planned_count <= 20:
        return planned_count
    return 3


def recommend_next(
    current_sets: list[dict],
    previous_sets: list[dict] | None,
    metadata: dict | None = None,
    *,
    set_count: int | None = None,
    historical_sets: list[dict] | None = None,
) -> dict:
    """Berechnet Gewicht und Wiederholungsziel, ohne bei Einbruch zu entlasten."""
    current = _working_sets(current_sets)
    previous = _working_sets(previous_sets)
    if not current:
        return {"weight_kg": None, "target_reps": [], "incremented": False}

    low, high, start = _rep_bounds(metadata)
    count = max(1, int(set_count)) if set_count else len(current)
    current_reps = [int(row.get("reps") or 0) for row in current[:count]]
    current_reps.extend([None] * max(0, count - len(current_reps)))
    weight = current[0].get("weight_kg")
    target = [start] * count
    incremented = False

    history = _working_sets([*(historical_sets or []), *previous])
    if history:
        def same_load(row):
            row_weight = row.get("weight_kg")
            if weight is None or row_weight is None:
                return weight is None and row_weight is None
            try:
                return math.isclose(float(row_weight), float(weight), abs_tol=0.001)
            except (TypeError, ValueError):
                return False

        # Warm-ups verbrauchen einen set_index; die Position zählt daher nur
        # unter den Arbeitssätzen derselben Einheit.
        ranks: dict = {}
        positions = []
        seen_rows: set[int] = set()
        unique_history = []
        for row in history:
            if id(row) not in seen_rows:  # "previous" steckt auch in "historical"
                seen_rows.add(id(row))
                unique_history.append(row)
        history = unique_history
        for offset, row in enumerate(history):
            workout_id = row.get("workout_id")
            if workout_id is None:
                positions.append(row.get("set_index") or offset + 1)
            else:
                ranks[workout_id] = ranks.get(workout_id, 0) + 1
                positions.append(ranks[workout_id])

        best_by_position: dict[int, int] = {}
        for row, position in zip(history, positions):
            if not same_load(row):
                continue
            try:
                reps = int(row.get("reps") or 0)
                position = int(position)
            except (TypeError, ValueError):
                continue
            best_by_position[position] = max(best_by_position.get(position, 0), reps)
        previous_goals = [
            min(high, max(low, best_by_position.get(index + 1, low)))
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

