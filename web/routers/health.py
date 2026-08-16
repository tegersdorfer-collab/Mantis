"""
Health — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request

from core import db
from domains.health_scores import compute_body_trend, health_narrative, score_history

from web.routers._helpers import _jsonable, _health_dict

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    def health(days: int = 14):
        if not orch:
            return []
        return [_health_dict(h) for h in orch._dashboard.get_recent_health(days=days)]

    @router.get("/api/health/scores")
    def health_scores(days: int = 90):
        """Domain-Scores (Recovery/Sleep/Activity) je Tag + 'heute'.

        Baseline über das ganze Fenster; degradiert ehrlich bei fehlenden Daten
        (status='insufficient_data'). Reiner Adapter über die Scoring-Engine.
        """
        if not orch:
            return {"days": [], "today": None}
        rows = [_health_dict(h) for h in orch._dashboard.get_recent_health(days=days)]
        hist = score_history(rows)
        body = compute_body_trend(rows)
        return {
            "days": hist,
            "today": hist[-1] if hist else None,
            "body": body,
            "narrative": health_narrative(hist[-1], body) if hist else None,
        }

    @router.post("/api/health/import")
    async def health_import():
        """Manueller Sync-Button in der PWA. Zieht COROS-Daten direkt (kein
        HealthKit-Push mehr)."""
        # Import steht bewusst in der Funktion: domains/coros/importer.py zieht
        # client.py und damit config nach, und dieses Modul importiert Router
        # bereits an anderer Stelle deferred (siehe body-Endpoints unten).
        from domains.coros import importer
        n = await asyncio.to_thread(importer.sync)
        return {"ok": True, "days": n}

    @router.post("/api/health/manual")
    async def health_manual(req: Request):
        d = await req.json()
        allowed = {"hrv", "resting_hr", "weight", "sleep_duration", "steps", "body_fat"}
        fields = {k: v for k, v in d.items() if k in allowed and v is not None}
        if not fields:
            return {"ok": False, "error": "Keine gültigen Felder"}
        date_str = d.get("date") or __import__("datetime").date.today().isoformat()
        cols = list(fields.keys())
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
        sql = (f"INSERT INTO health_data (date, {', '.join(cols)}, updated_at) "
               f"VALUES (%s, {', '.join(['%s']*len(cols))}, NOW()) "
               f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW()")
        db.execute(sql, tuple([date_str] + [fields[c] for c in cols]))
        return {"ok": True}

    @router.get("/api/body/measurements")
    def body_measurements(days: int = 90):
        from domains.body import get_recent
        return _jsonable(get_recent(days))

    @router.post("/api/body/measurements")
    async def body_log(req: Request):
        from domains.body import log_measurement
        body = await req.json()
        mid = log_measurement(**{k: v for k, v in body.items() if k != "date"})
        return {"id": mid}

    @router.get("/api/body/progress")
    def body_progress(weeks: int = 8):
        from domains.body import progress_summary
        return {"summary": progress_summary(weeks)}

    return router
