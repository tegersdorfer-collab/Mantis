"""
Health-Domäne (mantis-nativ).
Datenquelle: Swift-App pusht via POST /api/health/push (HealthKit background delivery).
Fallback-Poll alle 30 Minuten falls Push nicht kommt (HEALTH_API_URL).
"""
import logging

import httpx

from core import db
import config

log = logging.getLogger(__name__)

_R2 = lambda v: round(v * 100) / 100
_R1 = lambda v: round(v * 10) / 10

_last_updated: str | None = None  # Änderungserkennung via lastUpdated-Feld


def map_health_fields(data: dict) -> dict:
    """Reines Feld-Mapping: HealthKit/App-JSON → DB-Spalten (keine DB-Zugriffe).

    Kümmert sich um Einheiten-Umrechnung (min→h, SpO₂ 0-1 vs 0-100), Rundung und
    die diversen Schlüssel-Fallbacks (Swift- vs. Legacy-Feldnamen). Nur Felder mit
    Wert landen im Ergebnis.
    """
    fields: dict = {}

    def s(col, val):
        if val is not None:
            fields[col] = val

    s("steps",            _safe(data.get("steps"), round))
    s("active_calories",  _safe(data.get("activeEnergyKcal"), round))
    s("exercise_minutes", _safe(data.get("exerciseMinutes"), round))
    s("weight",           _safe(data.get("weightKg") or data.get("weight"), _R2))
    s("hrv",      _safe(data.get("hrv") or data.get("heartRateVariability"), _R2))
    s("vo2max",   _safe(data.get("vo2Max"), _R2))
    s("bmi",      _safe(data.get("bmi"), _R2))
    s("body_fat", _safe(data.get("bodyFatPercent"), _R2))
    # oxygenSaturationPercent ist schon 0-100; oxygenSaturation war 0-1
    spo2 = data.get("oxygenSaturationPercent") or data.get("oxygenSaturation")
    if spo2 is not None:
        s("blood_oxygen", _R2(spo2 if spo2 > 2 else spo2 * 100))
    s("body_temp", _safe(data.get("wristTemperature"), _R2))
    s("calories",  _safe(data.get("dietaryEnergy"), round))
    s("protein",   _safe(data.get("dietaryProtein"), _R1))
    s("carbs",     _safe(data.get("dietaryCarbohydrates"), _R1))
    s("fat",       _safe(data.get("dietaryFatTotal"), _R1))
    s("water",     _safe(data.get("water"), _R1))
    # distanceWalkingRunningKm (Swift) oder walkingDistanceKm (Legacy)
    s("distance",  _safe(data.get("distanceWalkingRunningKm") or data.get("walkingDistanceKm"), _R2))

    # Herzfrequenz
    s("resting_hr", _safe(data.get("restingHeartRate") or data.get("restingHr")
                          or data.get("resting_hr"), round))
    s("hr_avg",     _safe(data.get("heartRateAvg"), round))
    s("hr_max",     _safe(data.get("heartRateMax"), round))
    s("hr_min",     _safe(data.get("heartRateMin"), round))

    sleep = data.get("sleep") or {}
    s("sleep_duration", _safe(sleep.get("totalMinutes"),  lambda v: _R2(v / 60)))
    # Flaches App-Schema: sleepDuration/sleep_duration direkt in Stunden
    if "sleep_duration" not in fields:
        s("sleep_duration", _safe(data.get("sleepDuration") or data.get("sleep_duration"), _R2))
    s("sleep_deep",     _safe(sleep.get("deepMinutes"),   lambda v: _R2(v / 60)))
    s("sleep_rem",      _safe(sleep.get("remMinutes"),    lambda v: _R2(v / 60)))
    s("sleep_core",     _safe(sleep.get("coreMinutes"),   lambda v: _R2(v / 60)))
    in_bed = sleep.get("inBedMinutes")
    total  = sleep.get("totalMinutes")
    if in_bed is not None and total is not None:
        s("sleep_awake", _R2((in_bed - total) / 60))

    return fields


def process_health_data(data: dict) -> int:
    """
    Verarbeitet ein Health-JSON-Dict und schreibt es in die DB.
    Wird sowohl vom Pull (import_health) als auch vom Push-Endpoint genutzt.
    Gibt 1 bei erfolgreichem Schreiben zurück, 0 sonst.
    """
    global _last_updated

    last_updated = data.get("lastUpdated", "")
    if last_updated and last_updated == _last_updated:
        log.debug("🩺 Health: keine neuen Daten (lastUpdated unverändert)")
        return 0
    _last_updated = last_updated

    day = data.get("date")
    if not day:
        log.warning("Health-JSON enthält kein 'date'-Feld")
        return 0

    fields = map_health_fields(data)

    # Workouts aus HealthKit importieren (Swift liefert 'workouts'-Array)
    workouts_raw = data.get("workouts") or []
    if workouts_raw:
        try:
            from domains import fitness as _fitness
            imported = 0
            for w in workouts_raw:
                # Normalisierung der HealthKit-Workout-Activity-Typen
                hk_type = str(w.get("type") or w.get("workoutActivityType") or "other").lower()
                type_map = {
                    "running": "run", "cycling": "bike", "swimming": "swim",
                    "walking": "walk", "functionalstrengthtraining": "strength",
                    "traditionalstrengthtraining": "strength", "crosstraining": "strength",
                    "hiking": "hike", "yoga": "yoga", "pilates": "pilates",
                    "rowing": "row", "elliptical": "cardio", "stairstepping": "cardio",
                }
                workout_type = type_map.get(hk_type, "other")
                duration_min = None
                dur = w.get("durationMinutes") or w.get("duration")
                if dur is not None:
                    duration_min = round(float(dur))
                kcal = w.get("activeEnergyKcal") or w.get("activeEnergy")
                start = w.get("startDate") or w.get("date") or day
                try:
                    from datetime import date as _date
                    workout_date = _date.fromisoformat(str(start)[:10])
                except Exception:
                    workout_date = None
                title = w.get("title") or hk_type.capitalize()
                notes = f"HealthKit-Import: {round(float(kcal))} kcal" if kcal else "HealthKit-Import"
                _fitness.log_workout(
                    title=title, type_=workout_type,
                    duration_min=duration_min, notes=notes,
                    on_date=workout_date,
                )
                imported += 1
            if imported:
                log.info(f"🏋️ {imported} Workouts aus HealthKit importiert")
        except Exception as e:
            log.warning(f"Workout-Import fehlgeschlagen: {e}")

    if not fields:
        log.warning("Health-JSON: keine bekannten Felder gefunden")
        return 0 if not workouts_raw else 1

    cols = list(fields.keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO health_data (date, {', '.join(cols)}, updated_at) "
        f"VALUES (%s, {', '.join(['%s']*len(cols))}, NOW()) "
        f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW()"
    )
    db.execute(sql, tuple([day] + [fields[c] for c in cols]))
    log.info(f"🩺 Health: {day} geschrieben ({len(fields)} Felder, via {'push' if last_updated else 'pull'})")
    return 1


def upsert_day(day: str, fields: dict) -> bool:
    """Einen Tag in health_data schreiben. Formatunabhängig — wer die Felder
    erzeugt hat, ist nicht die Sache dieser Domäne.

    Idempotent über ON CONFLICT; leere Feld-Dicts werden nicht geschrieben.
    """
    if not day or not fields:
        return False
    cols = list(fields.keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO health_data (date, {', '.join(cols)}, updated_at) "
        f"VALUES (%s, {', '.join(['%s'] * len(cols))}, NOW()) "
        f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW()"
    )
    db.execute(sql, tuple([day] + [fields[c] for c in cols]))
    return True


def import_health() -> int:
    """Fallback-Poll: holt health_latest.json von der Swift-App."""
    url = getattr(config, "HEALTH_API_URL", "")
    if not url:
        return 0
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Health-Fetch fehlgeschlagen ({url}): {e}")
        return 0
    return process_health_data(data)


def _safe(val, fn):
    if val is None:
        return None
    try:
        return fn(val)
    except Exception:
        return None


def import_from_icloud() -> int:
    return import_health()


def recent(days: int = 7) -> list[dict]:
    return db.query(
        "SELECT * FROM health_data WHERE date >= CURRENT_DATE - %s ORDER BY date DESC",
        (days,),
    )


def latest() -> dict | None:
    return db.query_one("SELECT * FROM health_data ORDER BY date DESC LIMIT 1")


def history(days: int = 30) -> list[dict]:
    return db.query(
        "SELECT * FROM health_data WHERE date >= CURRENT_DATE - %s ORDER BY date",
        (days,),
    )
