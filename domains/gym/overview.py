"""Wochenstatistik, Trainingsstreak und Körpermessungen der Gym-App."""

from __future__ import annotations

from datetime import date, timedelta
import math

from core import db
from domains import body
from domains.gym.tracker import TrackerError


ALLOWED_PERIODS = {4, 12, 26}


def _measurement_value(row: dict | None) -> dict | None:
    if not row:
        return None
    measured_on = row.get("date")
    if measured_on is None:
        return None
    return {
        "date": measured_on.isoformat() if hasattr(measured_on, "isoformat") else str(measured_on),
        "weight_kg": float(row["weight_kg"]) if row.get("weight_kg") is not None else None,
        "body_fat": float(row["body_fat"]) if row.get("body_fat") is not None else None,
    }


def _latest_measurement() -> dict | None:
    row = db.query_one(
        """WITH measurements AS (
               SELECT date,weight_kg,body_fat,0 AS source_order
               FROM body_measurements
               WHERE weight_kg IS NOT NULL OR body_fat IS NOT NULL
               UNION ALL
               SELECT date,weight AS weight_kg,body_fat,1 AS source_order
               FROM health_data
               WHERE weight IS NOT NULL OR body_fat IS NOT NULL
           )
           SELECT MAX(date) AS date,
                  (SELECT weight_kg FROM measurements
                   WHERE weight_kg IS NOT NULL
                   ORDER BY date DESC,source_order ASC LIMIT 1) AS weight_kg,
                  (SELECT body_fat FROM measurements
                   WHERE body_fat IS NOT NULL
                   ORDER BY date DESC,source_order ASC LIMIT 1) AS body_fat
           FROM measurements"""
    )
    return _measurement_value(row)


def _streak_weeks(today: date) -> int:
    current_week = today - timedelta(days=today.weekday())
    rows = db.query(
        """SELECT DISTINCT date_trunc('week',date)::date AS week_start
           FROM workouts
           WHERE type IN ('gym','strength') AND ended_at IS NOT NULL AND date<=%s""",
        (today,),
    )
    completed_weeks = {row["week_start"] for row in rows}
    anchor = current_week
    if anchor not in completed_weeks:
        anchor -= timedelta(weeks=1)
    if anchor not in completed_weeks:
        return 0

    streak = 0
    while anchor in completed_weeks:
        streak += 1
        anchor -= timedelta(weeks=1)
    return streak


def dashboard(*, weeks: int = 4, today: date | None = None) -> dict:
    """Liefert abgeschlossene Workouts je Woche plus Profilkennzahlen."""
    if isinstance(weeks, bool) or weeks not in ALLOWED_PERIODS:
        raise TrackerError(400, "Wochenzeitraum muss 4, 12 oder 26 Wochen sein")
    day = today or date.today()
    if not isinstance(day, date):
        raise TrackerError(400, "Ungültiges Datum")
    current_week = day - timedelta(days=day.weekday())
    first_week = current_week - timedelta(weeks=weeks - 1)
    weekly_rows = db.query(
        """SELECT series.week_start::date AS week_start,
                  COUNT(DISTINCT w.id) AS workout_count,
                  COALESCE(SUM(CASE
                      WHEN COALESCE(ws.is_warmup,FALSE)=FALSE
                       AND COALESCE(e.load_mode,'external')<>'bodyweight'
                       AND ws.weight_kg>0 AND ws.reps>0
                      THEN ws.weight_kg*ws.reps*
                           CASE WHEN e.equipment='dumbbell'
                                     AND COALESCE(e.dumbbell_pair,TRUE)
                                THEN 2 ELSE 1 END
                      ELSE 0 END),0) AS volume_kg
           FROM generate_series(%s::date,%s::date,INTERVAL '1 week') AS series(week_start)
           LEFT JOIN workouts w
             ON date_trunc('week',w.date)::date=series.week_start::date
            AND w.type IN ('gym','strength') AND w.ended_at IS NOT NULL
           LEFT JOIN workout_sets ws ON ws.workout_id=w.id
           LEFT JOIN exercises e ON e.id=ws.exercise_id
           GROUP BY series.week_start ORDER BY series.week_start""",
        (first_week, current_week),
    )
    count_row = db.query_one(
        """SELECT COUNT(*) AS workout_count FROM workouts
           WHERE type IN ('gym','strength') AND ended_at IS NOT NULL"""
    )
    return {
        "workout_count": int((count_row or {}).get("workout_count") or 0),
        "streak_weeks": _streak_weeks(day),
        "weekly": [
            {
                "week_start": row["week_start"].isoformat(),
                "workout_count": int(row.get("workout_count") or 0),
                "volume_kg": round(float(row.get("volume_kg") or 0), 2),
            }
            for row in weekly_rows
        ],
        "latest_measurement": _latest_measurement(),
    }


def save_measurement(
    weight_kg: float | None,
    body_fat: float | None,
    *,
    date_: date | None = None,
) -> dict:
    """Validiert und speichert manuelle Werte in der vorhandenen Messtabelle."""
    if weight_kg is None and body_fat is None:
        raise TrackerError(400, "Mindestens Gewicht oder Körperfett ist erforderlich")
    if weight_kg is not None:
        try:
            weight = float(weight_kg)
        except (TypeError, ValueError):
            raise TrackerError(400, "Gewicht muss zwischen 20 und 500 kg liegen") from None
        if not math.isfinite(weight) or not 20 <= weight <= 500:
            raise TrackerError(400, "Gewicht muss zwischen 20 und 500 kg liegen")
    if body_fat is not None:
        try:
            fat = float(body_fat)
        except (TypeError, ValueError):
            raise TrackerError(400, "Körperfett muss zwischen 0 und 100 Prozent liegen") from None
        if not math.isfinite(fat) or not 0 <= fat <= 100:
            raise TrackerError(400, "Körperfett muss zwischen 0 und 100 Prozent liegen")
    measured_on = date_ or date.today()
    measurement_id = body.log_measurement(date_=measured_on, weight_kg=weight_kg, body_fat=body_fat)
    saved = body.get_on_date(measured_on)
    if saved is None:
        raise TrackerError(500, "Körpermessung konnte nicht geladen werden")
    return {"id": int(saved.get("id") or measurement_id), **_measurement_value(saved)}
