"""
Nutrition — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import config
from core import db
from core.jsonutil import extract_json
from domains import nutrition

from web.routers._helpers import _jsonable

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def _sum_food_items(data: dict) -> dict:
    """Summiert die vom Vision-Modell geschätzten Einzelkomponenten zu Gesamt-Makros.
    Robuster als das Modell selbst rechnen zu lassen. Fällt auf Top-Level-Werte zurück,
    falls keine 'items' geliefert wurden (altes flaches Format)."""
    if not isinstance(data, dict):
        return {}
    items = data.get("items")
    out = {
        "food_name": data.get("food_name") or data.get("name") or "Mahlzeit",
        "portion": data.get("portion") or "",
        "confidence": data.get("confidence"),
    }
    if isinstance(items, list) and items:
        def _s(key: str) -> float:
            total = 0.0
            for it in items:
                if isinstance(it, dict):
                    try:
                        total += float(it.get(key) or 0)
                    except (TypeError, ValueError):
                        pass
            return round(total, 1)
        out.update(calories=_s("calories"), protein=_s("protein"),
                   carbs=_s("carbs"), fat=_s("fat"), items=items)
    else:
        for k in ("calories", "protein", "carbs", "fat"):
            try:
                out[k] = float(data.get(k) or 0)
            except (TypeError, ValueError):
                out[k] = 0
    return out


_BG_TASKS: set = set()


async def _run_analysis(meal_id: int, image_bytes: bytes, annotation: str) -> None:
    """Hintergrund: Vision-Modell laufen lassen und die Pending-Mahlzeit füllen."""
    import base64 as _b64
    try:
        import ollama as _ollama
        b64 = _b64.standard_b64encode(image_bytes).decode()
        _client = _ollama.AsyncClient(host=config.OLLAMA_BASE_URL)
        vision_model = getattr(config, "VISION_MODEL", "qwen3.5:9b")
        prompt = (
            "Du bist ein Ernährungsexperte. Schätze die Nährwerte dieses Essens/Getränks "
            "so genau wie möglich.\n"
            + (f"Zusatzinfo vom Nutzer: {annotation}.\n" if annotation else "")
            + "Gehe so vor:\n"
            "1. Zerlege das Gericht in seine einzelnen Komponenten (z.B. Reis, Hähnchen, Soße).\n"
            "2. Schätze für JEDE Komponente das Gewicht in Gramm — nutze Teller, Besteck oder "
            "Hand als Größenreferenz. Lieber realistisch großzügig als zu klein.\n"
            "3. Berechne pro Komponente kcal/Protein/Kohlenhydrate/Fett anhand üblicher Nährwerte.\n"
            "Antworte NUR mit JSON (kein Text davor/danach), Einheiten kcal und Gramm:\n"
            '{"items":[{"name":"...","grams":0,"calories":0,"protein":0,"carbs":0,"fat":0}],'
            '"food_name":"Gesamtgericht","portion":"z.B. 1 großer Teller","confidence":0.0}'
        )
        # KEIN format="json" — das ließ qwen3-vl:8b leer antworten; der Prompt erzwingt JSON,
        # extract_json holt es robust raus. think=False: qwen3.5 würde sonst das
        # num_predict-Budget im Thinking verbrauchen und leeren Content liefern.
        resp = await _client.chat(
            model=vision_model,
            messages=[{"role": "user", "content": prompt, "images": [b64]}],
            options={"num_predict": 600, "temperature": 0.2, "num_ctx": 8192},
            think=False,
            keep_alive=0)
        data = _sum_food_items(extract_json((resp.message.content or "").strip(), default={}))
        if not data or not data.get("calories"):
            nutrition.fail_meal(meal_id)
            return
        name = annotation or data.get("food_name") or "Mahlzeit"
        nutrition.complete_meal(meal_id, name, data.get("calories"), data.get("protein"),
                                data.get("carbs"), data.get("fat"))
        log.info(f"Async-Foto-Analyse fertig (meal {meal_id})")
    except Exception:
        log.exception("Async-Foto-Analyse fehlgeschlagen")
        nutrition.fail_meal(meal_id)


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/nutrition")
    def nutrition_day(date_str: str = None):
        d = date.fromisoformat(date_str) if date_str else date.today()
        return {"meals": _jsonable(nutrition.meals_for(d)), "totals": _jsonable(nutrition.day_totals(d))}

    @router.post("/api/nutrition/analyze-photo")
    async def analyze_food_photo(req: Request):
        """Legt sofort eine Pending-Mahlzeit an und analysiert im Hintergrund."""
        try:
            form = await req.form()
            image_file = form.get("image")
            annotation = (form.get("text") or "").strip()
            if not image_file:
                return JSONResponse({"error": "kein Bild"}, 400)
            image_bytes = await image_file.read()
        except Exception as e:
            return JSONResponse({"error": f"Upload-Fehler: {e}"}, 400)
        mid = nutrition.create_pending_meal(annotation)
        task = asyncio.create_task(_run_analysis(mid, image_bytes, annotation))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return {"ok": True, "meal_id": mid, "status": "analyzing"}

    @router.post("/api/nutrition/log-meal")
    async def log_meal_from_app(req: Request):
        """Mahlzeit von iOS-App speichern."""
        d = await req.json()
        mid = nutrition.log_meal(
            description=d.get("name", "Mahlzeit"),
            meal_type="snack",
            calories=d.get("calories"),
            protein_g=d.get("protein"),
            carbs_g=d.get("carbs"),
            fat_g=d.get("fat"),
        )
        return {"ok": True, "id": mid}

    @router.get("/api/nutrition/history")
    def nutrition_history(days: int = 14):
        return _jsonable(nutrition.history(days))

    @router.post("/api/nutrition")
    async def add_meal(req: Request):
        d = await req.json()
        mid = nutrition.log_meal(description=d["description"], meal_type=d.get("meal_type", "snack"),
                                 calories=d.get("calories"), protein_g=d.get("protein_g"),
                                 carbs_g=d.get("carbs_g"), fat_g=d.get("fat_g"))
        return {"id": mid}

    @router.put("/api/nutrition/{mid}")
    async def nutrition_update(mid: int, req: Request):
        d = await req.json()
        nutrition.update_meal(mid, d.get("name"), d.get("calories"), d.get("protein"),
                              d.get("carbs"), d.get("fat"))
        return {"ok": True}

    @router.get("/api/nutrition/goals")
    def nutrition_goals():
        """Adaptiver Kalorie-Rechner für Bulk.
        Basis: BMR × Aktivitätsfaktor + Surplus.
        Anpassung: Gewichtstrend aus DB vs. Zielrate → ±kcal akkumuliert in settings.
        """
        HEIGHT_CM = 192
        AGE = 19
        WEIGHT_KG = 84        # Stargewicht / Fallback
        BULK_SURPLUS = 300
        ACTIVITY_FACTOR = 1.65
        TARGET_KG_PER_WEEK = 0.25   # sauberer Bulk
        ADJUST_STEP = 150            # kcal pro Anpassungsschritt
        MAX_ADJUSTMENT = 600         # maximale Gesamtabweichung vom Basis-Ziel

        # Aktuelles Gewicht: neuester DB-Eintrag
        w_row = db.query_one(
            "SELECT weight, date FROM health_data WHERE weight IS NOT NULL ORDER BY date DESC LIMIT 1"
        )
        current_weight = w_row["weight"] if w_row else WEIGHT_KG

        # BMR mit aktuellem Gewicht
        bmr = nutrition.bmr_mifflin(current_weight, HEIGHT_CM, AGE)
        tdee_base = bmr * ACTIVITY_FACTOR

        # Aktivitäts-Bonus heutiger Tag vs. 7-Tage-Schnitt
        act_rows = db.query(
            "SELECT active_calories FROM health_data "
            "WHERE date >= CURRENT_DATE - 7 AND active_calories IS NOT NULL ORDER BY date DESC LIMIT 7"
        )
        avg_active = sum(r["active_calories"] for r in act_rows) / len(act_rows) if act_rows else 350
        today_row = db.query_one("SELECT active_calories FROM health_data WHERE date = CURRENT_DATE")
        today_active = (today_row or {}).get("active_calories") or avg_active
        activity_bonus = max(0, today_active - avg_active)

        # ── Gewichtstrend-Analyse (lineare Regression) ───────────────────────
        w_rows = db.query(
            "SELECT date, weight FROM health_data WHERE weight IS NOT NULL "
            "AND date >= CURRENT_DATE - 60 ORDER BY date ASC"
        )
        trend_status = "insufficient_data"
        actual_kg_per_week = None
        trend_adjustment = int(db.get_setting("bulk_kcal_adjustment") or 0)

        if len(w_rows) >= 2:
            # Tage seit erstem Eintrag als x, Gewicht als y
            from datetime import date as _date
            dates = [r["date"] if isinstance(r["date"], _date) else _date.fromisoformat(str(r["date"])) for r in w_rows]
            weights = [float(r["weight"]) for r in w_rows]
            x0 = dates[0]
            xs = [(d - x0).days for d in dates]
            actual_kg_per_week = nutrition.linear_slope_per_week(xs, weights)
            if actual_kg_per_week is not None:
                span_days = (dates[-1] - dates[0]).days
                if span_days >= 14:
                    trend_status, new_adj = nutrition.bulk_adjustment(
                        actual_kg_per_week, TARGET_KG_PER_WEEK,
                        trend_adjustment, ADJUST_STEP, MAX_ADJUSTMENT,
                    )
                    # Nur speichern wenn sich etwas geändert hat
                    if new_adj != trend_adjustment:
                        db.set_setting("bulk_kcal_adjustment", str(new_adj))
                        trend_adjustment = new_adj
                else:
                    trend_status = "not_enough_span"

        tdee = tdee_base + activity_bonus
        kcal_goal = round(tdee + BULK_SURPLUS + trend_adjustment)

        # Makros: 2.2g P/kg, 1.0g F/kg, Rest Carbs
        _macros = nutrition.macros_for(kcal_goal, current_weight)
        protein_g, fat_g, carbs_g = _macros["protein"], _macros["fat"], _macros["carbs"]

        return {
            "kcal": kcal_goal,
            "protein": protein_g,
            "carbs": max(carbs_g, 50),
            "fat": fat_g,
            "meta": {
                "bmr": round(bmr),
                "current_weight": current_weight,
                "activity_factor": ACTIVITY_FACTOR,
                "tdee_base": round(tdee_base),
                "activity_bonus": round(activity_bonus),
                "tdee": round(tdee),
                "surplus": BULK_SURPLUS,
                "trend_adjustment": trend_adjustment,
                "trend_status": trend_status,
                "actual_kg_per_week": actual_kg_per_week,
                "target_kg_per_week": TARGET_KG_PER_WEEK,
            }
        }

    @router.post("/api/nutrition/goals/reset-adjustment")
    def reset_bulk_adjustment():
        """Setzt die akkumulierte Kalorie-Anpassung zurück."""
        db.set_setting("bulk_kcal_adjustment", "0")
        return {"ok": True}

    return router
