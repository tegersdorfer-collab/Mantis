"""
Fitness — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core import db
from core.jsonutil import extract_json
from domains import habits, fitness
from domains import plan_generator

from web.routers._helpers import _jsonable

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/workouts")
    def workouts(limit: int = 20):
        return _jsonable(fitness.recent_workouts(limit))

    @router.post("/api/workouts")
    async def add_workout(req: Request):
        d = await req.json()
        wid = fitness.log_workout(
            title=d["title"], type_=d.get("type", "strength"),
            duration_min=d.get("duration_min"), distance_km=d.get("distance_km"),
            notes=d.get("notes"), rpe=d.get("rpe"), sets=d.get("sets"))
        t = (d.get("type") or "").lower()
        if t in ("lower", "upper"):
            fitness.record_cycle_event(t, "workout")
            habits.log_sport_done()
        return {"id": wid}

    @router.get("/api/fitness/volume")
    def volume():
        return _jsonable(fitness.weekly_volume())

    @router.get("/api/fitness/volume_by_day")
    def volume_by_day(days: int = 14):
        return _jsonable(fitness.volume_by_day(days))

    @router.get("/api/fitness/muscles")
    def muscles(days: int = 7):
        return fitness.muscle_volume(days)

    @router.get("/api/fitness/exercises")
    def exercises():
        return _jsonable(fitness.list_exercises())

    @router.get("/api/fitness/plan")
    def get_plan():
        return _jsonable(fitness.active_plan()) or {}

    @router.get("/api/fitness/today-plan")
    def today_plan():
        """Heutiger Trainingsplan: abschluss-basierter Zyklus LOWER → JOGGEN → UPPER,
        moduliert über HRV+Schlaf (Intensität) und Progressive Overload."""
        from datetime import date as _date
        import math as _math

        events = fitness.recent_cycle_events(limit=12)
        state = fitness.cycle_state(events, _date.today())
        day_type = state["slot"]
        done_today = state["done_today"]

        health = db.query_one("SELECT * FROM health_data ORDER BY date DESC LIMIT 1") or {}
        hrv = float(health.get("hrv_avg") or 0)
        sleep_h = float(health.get("sleep_hours") or 0)
        hrv_score = min(hrv / 70.0, 1.2) if hrv > 0 else 1.0
        sleep_score = min(sleep_h / 8.0, 1.1) if sleep_h > 0 else 1.0
        intensity = round(max(0.85, min(1.05, (hrv_score + sleep_score) / 2.0)), 2)

        mantis_note = ""
        if hrv > 0 and sleep_h > 0 and day_type != "jog":
            if intensity >= 1.0:
                mantis_note = f"HRV {hrv:.0f}, Schlaf {sleep_h:.1f}h — top Werte, Gewichte leicht erhöhen."
            elif intensity <= 0.88:
                mantis_note = f"HRV {hrv:.0f}, Schlaf {sleep_h:.1f}h — Erholung niedrig, Gewichte reduzieren."
            else:
                mantis_note = f"HRV {hrv:.0f}, Schlaf {sleep_h:.1f}h — normale Session."

        def last_set(exercise_name: str) -> dict | None:
            return db.query_one(
                """SELECT ws.weight_kg, ws.reps FROM workout_sets ws
                   JOIN exercises e ON e.id = ws.exercise_id
                   WHERE LOWER(e.name) = LOWER(%s)
                   ORDER BY ws.id DESC LIMIT 1""",
                (exercise_name,),
            )

        def build_sets(exercise_name: str, default_weight: float, default_reps: int,
                       working_count: int = 3, rpe_target: int = 7) -> dict:
            prev = last_set(exercise_name)
            if prev and prev.get("weight_kg"):
                base_w = float(prev["weight_kg"]) * intensity
                base_r = prev.get("reps") or default_reps
            else:
                base_w = default_weight * intensity
                base_r = default_reps
            w = _math.floor(base_w / 2.5) * 2.5
            warmup = [
                {"weight": round(w * 0.4, 1), "reps": 12},
                {"weight": round(w * 0.6, 1), "reps": 8},
                {"weight": round(w * 0.8, 1), "reps": 5},
            ]
            working = [{"weight": w, "reps": base_r, "rpe_target": rpe_target}] * working_count
            # Pause je nach Wiederholungsbereich: schwere Sätze brauchen länger
            rest_sec = 180 if base_r <= 5 else 150 if base_r <= 8 else 90 if base_r <= 12 else 60
            return {"name": exercise_name, "warmup_sets": warmup, "working_sets": working,
                    "rest_sec": rest_sec}

        plan_row = fitness.active_plan()
        plan_json = plan_row.get("plan_json") if plan_row else None

        def _variant(slot: str) -> str:
            return plan_generator.pick_variant(fitness.slot_workout_count(slot))

        variant = _variant(day_type) if day_type in ("lower", "upper") else "A"
        plan_key = f"{day_type}{variant}"
        plan_source = "mantis" if isinstance(plan_json, dict) and plan_json.get(plan_key) else "default"
        plan_week = None
        if plan_source == "mantis":
            created = plan_row.get("created_at")
            if created is not None:
                from datetime import datetime as _dt2, date as _date2
                cd = created.date() if isinstance(created, _dt2) else created
                if isinstance(cd, _date2):
                    plan_week = min(6, (_date.today() - cd).days // 7 + 1)

        def _block(slot: str) -> list[dict]:
            key = f"{slot}{_variant(slot)}"
            src = plan_json.get(key) if isinstance(plan_json, dict) and plan_json.get(key) \
                else plan_generator.DEFAULT_PLAN[slot]
            return [build_sets(ex["name"], float(ex.get("weight") or 20), int(ex.get("reps") or 8),
                               working_count=int(ex.get("sets") or 3),
                               rpe_target=int(ex.get("rpe") or 7)) for ex in src]

        if day_type == "lower":
            exercises_list = _block("lower")
        elif day_type == "upper":
            exercises_list = _block("upper")
        else:  # jog
            exercises_list = []
            mantis_note = "Heute: Joggen — läuft über Strava."

        day_label = fitness.CYCLE_LABEL[day_type]
        if day_type in ("lower", "upper"):
            day_label = f"{day_label} · {variant}"

        return {
            "day_type": day_type,
            "day_label": day_label,
            "intensity_factor": intensity,
            "done_today": done_today,
            "next_label": state["next_label"],
            "plan_week": plan_week,
            "plan_source": plan_source,
            "mantis_message": mantis_note or f"Heute: {fitness.CYCLE_LABEL[day_type]}.",
            "health": {
                "hrv": hrv or None,
                "sleep_hours": sleep_h or None,
                "date": str(health.get("date", "")),
            },
            "exercises": exercises_list,
        }

    @router.post("/api/fitness/log-rpe")
    async def log_rpe(req: Request):
        """RPE-Feedback nach einer Übung speichern."""
        d = await req.json()
        workout_id = d.get("workout_id")
        exercise_name = d.get("exercise")
        rpe = d.get("rpe")
        if workout_id and rpe is not None:
            db.execute(
                "UPDATE workout_sets SET rpe = %s WHERE workout_id = %s AND exercise_id = "
                "(SELECT id FROM exercises WHERE LOWER(name) = LOWER(%s) LIMIT 1)",
                (rpe, workout_id, exercise_name or ""),
            )
        return {"ok": True}

    @router.post("/api/fitness/import")
    async def import_workout(req: Request):
        """Trainings-Import. Strukturierter Gym-App-Export → deterministischer Parser;
        freier Text → KI-Parsing."""
        d = await req.json()
        dump = d.get("text", "").strip()
        if not dump:
            return JSONResponse({"error": "kein Text"}, 400)

        # Strukturiertes Gym-App-CSV erkennen (kein LLM nötig, schnell)
        if "#;KG;REPS" in dump or "#;KM;" in dump or "#;KG;SECS" in dump:
            try:
                res = await asyncio.to_thread(fitness.import_workout_csv, dump)
                return {"ok": True, **res}
            except Exception:
                log.exception("CSV-Parsing fehlgeschlagen")
                return JSONResponse({"error": "CSV-Parsing fehlgeschlagen."}, 422)

        if not orch:
            return JSONResponse({"error": "kein Kern"}, 503)

        prompt = (
            "Parse diesen Trainings-Dump in JSON. Format:\n"
            '{"title":"...","type":"strength|run|mobility","duration_min":null,'
            '"distance_km":null,"exercises":[{"name":"...","sets":[{"reps":10,"weight_kg":60}]}]}\n'
            "Nur JSON, kein Text drumherum.\n\nDump:\n" + dump + "\n\nJSON:"
        )
        raw = await orch.bg_llm.chat(messages=[{"role": "user", "content": prompt}],
                                  temperature=0.2, max_tokens=900, format="json")
        data = extract_json(raw, default=None)
        if not isinstance(data, dict):
            return JSONResponse({"error": "Parsing fehlgeschlagen", "raw": raw[:300]}, 422)
        flat = []
        for ex in data.get("exercises", []):
            for i, st in enumerate(ex.get("sets", []), 1):
                flat.append({"exercise": ex.get("name"), "set_index": i,
                             "reps": st.get("reps"), "weight_kg": st.get("weight_kg")})
        wid = fitness.log_workout(
            title=data.get("title", "Training"), type_=data.get("type", "strength"),
            duration_min=data.get("duration_min"), distance_km=data.get("distance_km"),
            sets=flat)
        return {"id": wid, "parsed": data}

    @router.post("/api/fitness/jog-done")
    async def jog_done(req: Request):
        """Markiert den heutigen Jog-Tag als erledigt (idempotent pro Tag)."""
        try:
            d = await req.json()
        except Exception:
            d = {}
        if fitness.jog_done_today_exists():
            return {"ok": True, "already": True}
        fitness.record_cycle_event("jog", "jog")
        habits.log_sport_done()
        return {"ok": True, "source": (d or {}).get("source", "manual")}

    @router.post("/api/fitness/rest-day")
    async def rest_day(req: Request):
        """Schiebt einen Ruhetag ein — Zeiger bleibt auf dem aktuellen Slot."""
        from datetime import date as _date
        events = fitness.recent_cycle_events(limit=12)
        state = fitness.cycle_state(events, _date.today())
        fitness.record_cycle_event(state["slot"], "rest")
        return {"ok": True, "slot": state["slot"]}

    @router.get("/api/workouts/{wid}")
    def workout_detail(wid: int):
        w = db.query_one("SELECT * FROM workouts WHERE id=%s", (wid,))
        if not w:
            return JSONResponse({"error": "not found"}, 404)
        w["sets"] = db.query(
            """SELECT ws.id, ws.set_index, ws.reps, ws.weight_kg, ws.rpe,
                      COALESCE(ws.is_warmup,FALSE) AS is_warmup,
                      COALESCE(ws.is_failure,FALSE) AS is_failure, e.name AS exercise
               FROM workout_sets ws LEFT JOIN exercises e ON e.id=ws.exercise_id
               WHERE ws.workout_id=%s ORDER BY ws.set_index""", (wid,))
        return _jsonable(w)

    @router.put("/api/workouts/{wid}")
    async def workout_update(wid: int, req: Request):
        d = await req.json()
        fitness.update_workout(wid, title=d.get("title"), notes=d.get("notes"),
                               rpe=d.get("rpe"), sets=d.get("sets"))
        return {"ok": True}

    @router.delete("/api/workouts/{wid}")
    def workout_delete(wid: int):
        fitness.delete_workout(wid)
        return {"ok": True}

    @router.get("/api/fitness/last-sets")
    def last_sets(exercise: str = ""):
        return _jsonable(fitness.last_sets_for(exercise)) if exercise else []

    @router.get("/api/fitness/profile")
    def get_profile():
        return fitness.get_training_profile()

    @router.put("/api/fitness/profile")
    async def put_profile(req: Request):
        try:
            d = await req.json()
        except Exception:
            d = {}
        return fitness.save_training_profile(d if isinstance(d, dict) else {})

    @router.post("/api/fitness/plan/generate")
    async def generate_plan(req: Request):
        if not orch:
            return JSONResponse({"error": "kein Kern"}, 503)
        plan = await plan_generator.generate_and_save(None, orch.bg_llm)
        if not plan:
            return JSONResponse({"ok": False, "error": "Generierung fehlgeschlagen"}, 502)
        return {"ok": True, "plan": plan}

    return router
