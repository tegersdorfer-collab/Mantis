"""
System — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

import config
from core import db, tools as T, backup
from core.skill_factory import delete_skill, create_skill
from domains import habits, fitness, nutrition, journal, goals, weather, tasks as tasks_d
from domains.self_modify import write_file

from web.routers._helpers import _jsonable, _health_dict, _event_dict

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def _daily_usage_window(
    days: int, now: datetime, timezone_name: str
) -> tuple[datetime, datetime]:
    """Gibt das lokale Tagesfenster als UTC-Grenzen zurück, DST-sicher."""
    owner_tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(owner_tz) if now.tzinfo else now.replace(tzinfo=owner_tz)
    first_day = local_now.date() - timedelta(days=days - 1)
    after_last_day = local_now.date() + timedelta(days=1)
    start_local = datetime.combine(first_day, dt_time.min, tzinfo=owner_tz)
    end_local = datetime.combine(after_last_day, dt_time.min, tzinfo=owner_tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _aggregate_daily_usage(
    rows: list[dict], start: datetime, end: datetime, timezone_name: str
) -> list[dict]:
    """Aggregiert einzelne Nachrichten nach Besitzer-Tag und Kanal."""
    owner_tz = ZoneInfo(timezone_name)
    counts = Counter()
    for row in rows:
        if row.get("role") != "user":
            continue
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_utc = created_at.astimezone(timezone.utc)
        if not start <= created_at_utc < end:
            continue
        counts[(created_at_utc.astimezone(owner_tz).date(), row["channel"])] += 1

    return [
        {"date": day.isoformat(), "channel": channel, "user_messages": count}
        for (day, channel), count in sorted(counts.items())
    ]


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/status")
    def status():
        pid = None; running = False
        try:
            pid = int(open("/tmp/mantis.pid").read().strip())
            os.kill(pid, 0); running = True
        except Exception:
            pass
        active_model = "Cloud-LLM"
        if orch is not None:
            try:
                active_model = orch.chat_llm.model_name
            except Exception:
                pass
        return {"running": running or orch is not None, "pid": pid,
                "model": active_model, "tools": len(T.REGISTRY),
                "time": datetime.now().strftime("%H:%M:%S")}

    @router.get("/api/usage/daily")
    def daily_usage(days: int = Query(default=56, ge=1)):
        owner_timezone = getattr(config, "OWNER_TIMEZONE", "Europe/Berlin")
        start, end = _daily_usage_window(
            days, datetime.now(ZoneInfo(owner_timezone)), owner_timezone
        )
        rows = db.query(
            """
            SELECT role, channel, created_at
            FROM chat_messages
            WHERE role = 'user'
              AND created_at >= %s
              AND created_at < %s
            ORDER BY created_at, channel
            """,
            (start, end),
        )
        return {"days": _aggregate_daily_usage(rows, start, end, owner_timezone)}

    @router.get("/api/overview")
    def overview():
        out = {}
        try:
            h = orch._dashboard.get_recent_health(days=2) if orch else []
            out["health"] = [_health_dict(x) for x in h]
        except Exception:
            out["health"] = []
        try:
            out["habits"] = habits.habit_overview()
        except Exception:
            out["habits"] = []
        try:
            out["tasks"] = _jsonable(tasks_d.list_tasks("open"))
        except Exception:
            out["tasks"] = []
        try:
            out["events"] = [_event_dict(e) for e in orch._dashboard.get_upcoming_events(7)] if orch else []
        except Exception:
            out["events"] = []
        try:
            out["goals"] = goals.list_goals()
        except Exception:
            out["goals"] = []
        try:
            out["nutrition"] = nutrition.day_totals()
        except Exception:
            out["nutrition"] = {}
        try:
            out["fitness"] = fitness.weekly_volume()
        except Exception:
            out["fitness"] = {}
        return out

    @router.get("/api/backups")
    def get_backups():
        return backup.list_backups()

    @router.post("/api/backups/run")
    async def trigger_backup():
        result = await asyncio.to_thread(backup.run_backup)
        return result

    @router.get("/api/push/vapid-public-key")
    def push_vapid_key():
        return {"key": config.VAPID_PUBLIC_KEY}

    @router.post("/api/push/subscribe")
    async def push_subscribe(req: Request):
        d = await req.json()
        from core import push as _push
        keys = d.get("keys", {})
        _push.add_subscription(d["endpoint"], keys.get("p256dh", ""), keys.get("auth", ""))
        return {"ok": True}

    @router.post("/api/push/unsubscribe")
    async def push_unsubscribe(req: Request):
        d = await req.json()
        from core import push as _push
        _push.remove_subscription(d["endpoint"])
        return {"ok": True}

    @router.post("/api/push/test")
    async def push_test():
        from core import push as _push
        n = await asyncio.to_thread(_push.send_push, "Mantis", "Test-Benachrichtigung — Push funktioniert! 🎉", "/")
        return {"ok": True, "sent": n}

    @router.get("/api/self-changes")
    def self_changes(limit: int = 50):
        rows = db.query(
            "SELECT id, type, summary, detail, created_at FROM events_log "
            "WHERE type = 'self_modify' ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        out = []
        for r in rows:
            d = r["detail"] or {}
            out.append({
                "id": r["id"], "summary": r["summary"], "kind": d.get("kind"),
                "path": d.get("path"), "commit": d.get("commit"),
                "created_at": r["created_at"].isoformat(),
            })
        return out

    @router.get("/api/self-changes/{change_id}")
    def self_change_detail(change_id: int):
        row = db.query_one(
            "SELECT id, summary, detail, created_at FROM events_log WHERE id=%s AND type='self_modify'",
            (change_id,),
        )
        if not row:
            return JSONResponse({"error": "Nicht gefunden"}, 404)
        d = row["detail"] or {}
        return {
            "id": row["id"], "summary": row["summary"], "created_at": row["created_at"].isoformat(),
            "kind": d.get("kind"), "path": d.get("path"), "commit": d.get("commit"),
            "diff": d.get("diff", ""),
        }

    @router.post("/api/self-changes/{change_id}/revert")
    def self_change_revert(change_id: int):
        row = db.query_one(
            "SELECT detail FROM events_log WHERE id=%s AND type='self_modify'", (change_id,)
        )
        if not row:
            return JSONResponse({"error": "Nicht gefunden"}, 404)
        d = row["detail"] or {}
        kind, path = d.get("kind"), d.get("path")
        old_content = d.get("old_content", "")

        try:
            if kind == "skill_create" or kind == "skill_delete":
                skill_name = Path(path).stem
                if kind == "skill_create":
                    result = delete_skill(skill_name)
                else:
                    src = old_content.split('"""\n', 2)
                    body = src[2] if len(src) == 3 else old_content
                    result = create_skill(skill_name, f"Revert von Löschung (Change #{change_id})", body)
                return result

            if kind == "code_write":
                result = write_file(path, old_content, f"Revert von Change #{change_id}")
                return result

            return {"ok": False, "message": "Unbekannte Änderungsart, kann nicht automatisch rückgängig gemacht werden."}
        except Exception:
            log.exception("Fehler beim Revert von Change #%s", change_id)
            return JSONResponse({"ok": False, "message": "Revert fehlgeschlagen."}, 500)

    @router.get("/api/weather")
    async def api_weather():
        return await weather.get_weather()

    @router.get("/api/usage")
    def api_usage(days: int = 30):
        """Token-Verbrauch + API-Kosten (Tages-Aggregate, pro Modell, Summen)."""
        from core import llm_usage
        return llm_usage.summary(max(1, min(days, 365)))

    @router.get("/api/errors/today")
    def errors_today():
        rows = db.query(
            "SELECT summary, detail, created_at FROM events_log "
            "WHERE type='error' AND created_at >= CURRENT_DATE ORDER BY created_at DESC"
        )
        return {"count": len(rows), "errors": _jsonable(rows)}

    @router.get("/api/tools")
    def tools_list():
        return [{"name": t.name, "category": t.category, "description": t.description}
                for t in T.REGISTRY.values()]

    @router.get("/api/analytics")
    def analytics():
        out = {}
        if orch:
            try:
                out["health"] = [_health_dict(h) for h in orch._dashboard.get_recent_health(days=30)]
            except Exception:
                out["health"] = []
        out["mood"] = _jsonable(journal.mood_trend(30))
        out["workouts_30d"] = db.query_one("SELECT COUNT(*) c FROM workouts WHERE date >= CURRENT_DATE - 30") or {"c": 0}
        out["habits"] = habits.habit_overview(30)
        return _jsonable(out)

    @router.get("/api/settings")
    def get_settings():
        return {
            "weather_city": db.get_setting("weather_city", "Berlin"),
            "proactive_interval": db.get_setting("proactive_interval_override", config.PROACTIVE_INTERVAL),
            "meta_notes": db.get_setting("meta_notes", []),
        }

    @router.post("/api/settings")
    async def set_settings(req: Request):
        d = await req.json()
        for k, v in d.items():
            db.set_setting(k, v)
        return {"ok": True}

    @router.get("/api/subagents")
    def list_subagents():
        from tools.delegate import get_active_subagents
        return get_active_subagents()

    return router
