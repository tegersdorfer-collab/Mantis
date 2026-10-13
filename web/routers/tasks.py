"""
Tasks — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core import db
from core.timeparse import parse_datetime
from domains import tasks as tasks_d
from domains.task_executor import classify, learn_from_rejection, suggest_one

from web.routers._helpers import _jsonable

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks")
    def get_tasks(status: str = "open"):
        return _jsonable(tasks_d.list_tasks(status))

    @router.post("/api/tasks")
    async def create_task(req: Request):
        d = await req.json()

        due = parse_datetime(d["due"]) if d.get("due") else None
        tid = tasks_d.create_task(title=d["title"], priority=d.get("priority", "medium"),
                                  kind=d.get("kind", "task"), due=due,
                                  notes=d.get("notes"), parent_id=d.get("parent_id"))
        # Zuweisung: explizit > auto-klassifizieren
        explicit = d.get("assigned_to")
        if explicit in ("user", "mantis"):
            db.execute("UPDATE tasks SET assigned_to=%s WHERE id=%s", (explicit, tid))
        elif orch:
            # Auto-Klassifikation ist optional — Fehler darf die Task-Erstellung
            # nicht kippen (Task existiert bereits), aber wird geloggt statt geschluckt.
            try:
                assignee = await classify(d["title"], d.get("notes"), orch.bg_llm)
                db.execute("UPDATE tasks SET assigned_to=%s WHERE id=%s", (assignee, tid))
            except Exception as e:
                log.warning(f"Task-Auto-Klassifikation fehlgeschlagen (Task {tid}): {e}")
        return {"id": tid}

    @router.get("/api/tasks/suggestions")
    def get_suggestions():
        rows = db.query(
            "SELECT * FROM tasks WHERE suggestion_status='proposed' ORDER BY created_at DESC"
        )
        return _jsonable(rows)

    @router.post("/api/tasks/{tid}/accept")
    def accept_suggestion(tid: int):
        db.execute(
            "UPDATE tasks SET suggestion_status='accepted', status='todo' WHERE id=%s", (tid,)
        )
        return {"ok": True}

    @router.post("/api/tasks/{tid}/reject")
    async def reject_suggestion(tid: int, req: Request):
        d = await req.json()
        reason = d.get("reason", "")
        db.execute(
            "UPDATE tasks SET suggestion_status='rejected', rejection_reason=%s, status='archived' WHERE id=%s",
            (reason, tid)
        )
        # Mantis lernt daraus
        if orch and reason:
            async def _learn():
                try:
                    await learn_from_rejection(tid, reason, orch.lzg, orch.embed_llm)
                except Exception as e:
                    log.warning(f"learn_from_rejection fehlgeschlagen: {e}")
            asyncio.create_task(_learn())
        return {"ok": True}

    @router.post("/api/tasks/generate")
    async def generate_task_suggestion():
        """Manueller Trigger: Mantis überlegt sich sofort einen neuen Task-Vorschlag."""
        if not orch:
            return {"ok": False, "error": "Kein Orchestrator"}
        import asyncio as _aio
        _aio.create_task(suggest_one("Manueller Trigger durch Timo im Hub", orch.bg_llm, orch.lzg))
        return {"ok": True}

    @router.post("/api/tasks/{tid}/complete")
    def complete_task(tid: int):
        tasks_d.complete_task(tid); return {"ok": True}

    @router.post("/api/tasks/{tid}/progress")
    async def task_progress(tid: int, req: Request):
        d = await req.json()
        tasks_d.set_progress(tid, int(d.get("progress_pct", 0))); return {"ok": True}

    @router.post("/api/tasks/{tid}/status")
    async def task_status(tid: int, req: Request):
        d = await req.json()
        tasks_d.set_status(tid, d.get("status", "todo")); return {"ok": True}

    @router.post("/api/tasks/{tid}/archive")
    def task_archive(tid: int):
        tasks_d.archive_task(tid); return {"ok": True}

    @router.delete("/api/tasks/{tid}")
    def task_delete(tid: int):
        tasks_d.delete_task(tid); return {"ok": True}

    @router.get("/api/tasks/top3")
    def tasks_top3():
        rows = db.query(
            "SELECT * FROM tasks WHERE today_focus=TRUE AND status NOT IN ('done','archived') "
            "ORDER BY priority DESC, due ASC LIMIT 3"
        )
        return rows

    @router.put("/api/tasks/{task_id}/top3")
    async def toggle_top3(task_id: int, req: Request):
        d = await req.json()
        focus = bool(d.get("focus", True))
        if focus:
            count = db.query_one("SELECT COUNT(*) as n FROM tasks WHERE today_focus=TRUE "
                                  "AND status NOT IN ('done','archived')")
            if count and count["n"] >= 3:
                return JSONResponse({"error": "Maximal 3 Top-Tasks erlaubt"}, status_code=400)
        db.execute("UPDATE tasks SET today_focus=%s WHERE id=%s", (focus, task_id))
        return {"ok": True}

    @router.get("/api/daily-resurface")
    def daily_resurface():
        """Gibt täglich eine zufällige alte Notiz/Journal/Memory zurück."""
        from datetime import date as _date
        import hashlib
        day_seed = int(hashlib.md5(_date.today().isoformat().encode()).hexdigest(), 16)

        # Brain-Note (älter als 7 Tage, pinned bevorzugt)
        rows = db.query(
            "SELECT title, content, 'brain' as source FROM brain_notes "
            "WHERE status='active' AND created_at < NOW() - INTERVAL '7 days' "
            "ORDER BY pinned DESC, created_at ASC LIMIT 20"
        )
        if not rows:
            rows = db.query(
                "SELECT title, content, 'brain' as source FROM brain_notes "
                "WHERE status='active' ORDER BY created_at ASC LIMIT 20"
            )
        if not rows:
            # Fallback: Journal
            rows = db.query(
                "SELECT title, content, 'journal' as source FROM journal_entries "
                "ORDER BY created_at ASC LIMIT 20"
            )
        if not rows:
            return {"title": "", "content": "", "source": ""}
        item = rows[day_seed % len(rows)]
        return {"title": item["title"], "content": (item["content"] or "")[:300], "source": item["source"]}

    @router.get("/api/tasks/slipping")
    def tasks_slipping(days: int = 5):
        """Offene Tasks, die seit mehr als `days` Tagen liegen (kein updated_at → created_at)."""
        rows = db.query(
            """
            SELECT id, title, priority, due, created_at
            FROM tasks
            WHERE status NOT IN ('done','archived')
              AND created_at < NOW() - make_interval(days => %s)
            ORDER BY created_at ASC
            LIMIT 10
            """,
            (days,),
        )
        return rows

    return router
