"""
Chat — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from core import db
from core.status import BUS
from core.stream_events import StreamEvent

from web.routers._helpers import _jsonable

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/chat/history")
    def chat_history(limit: int = 50):
        rows = db.query("SELECT role, content, channel, created_at FROM chat_messages ORDER BY created_at DESC LIMIT %s", (limit,))
        return _jsonable(list(reversed(rows)))

    @router.post("/api/chat")
    async def chat(req: Request):
        d = await req.json()
        if not orch:
            return {"response": "Mantis-Kern nicht verbunden."}
        channel_name = d.get("channel", "web")
        if not isinstance(channel_name, str) or channel_name not in {"web", "ios", "mcp"}:
            channel_name = "web"
        resp, trace = await orch.dashboard_respond(d["text"], channel_name=channel_name)
        return {"response": resp, "tools": [t["tool"] for t in trace]}

    @router.get("/api/chat/stream")
    async def chat_stream(text: str):
        if not orch:
            return JSONResponse({"error": "kein Kern"}, 503)
        q: asyncio.Queue = asyncio.Queue()

        partials: dict[str, str] = {}

        async def cb(event: StreamEvent):
            if event.kind == "attempt_start":
                partials[event.attempt_id] = ""
                await q.put({"partial": ""})
            elif event.kind == "delta":
                partials[event.attempt_id] = partials.get(event.attempt_id, "") + event.text_delta
                await q.put({"partial": partials[event.attempt_id]})
            elif event.kind == "attempt_abort":
                partials.pop(event.attempt_id, None)
                await q.put({"partial": ""})

        async def run():
            try:
                resp, trace = await orch.dashboard_respond(text, stream_cb=cb, channel_name="web")
                await q.put({"done": resp, "tools": [t["tool"] for t in trace]})
            except Exception as e:
                await q.put({"done": f"Fehler: {e}", "tools": []})

        task = asyncio.create_task(run())

        async def gen():
            try:
                while True:
                    item = await q.get()
                    yield f"data: {json.dumps(item)}\n\n"
                    if "done" in item:
                        break
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/api/status/stream")
    async def status_stream():
        """SSE-Stream mit Echtzeit-Status-Updates von Mantis."""
        q = BUS.subscribe()

        async def gen():
            import json as _json
            try:
                # Sofort aktuellen Status senden
                yield f"data: {_json.dumps(BUS.current.to_dict())}\n\n"
                # Letzte N Events als Replay (wenige – Client dedupliziert)
                for evt in BUS.history[-5:]:
                    yield f"data: {_json.dumps(evt.to_dict())}\n\n"
                # Live-Updates
                while True:
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=25)
                        yield f"data: {_json.dumps(evt.to_dict())}\n\n"
                    except asyncio.TimeoutError:
                        yield "data: {\"keepalive\":true}\n\n"
            finally:
                BUS.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.get("/api/status/current")
    def status_current():
        """Aktueller Status als JSON (für initiales Laden)."""
        return BUS.current.to_dict()

    @router.get("/api/feed/stream")
    async def feed_stream():
        async def gen():
            row = await asyncio.to_thread(db.query_one, "SELECT MAX(id) m FROM events_log")
            last_id = (row["m"] or 0) if row else 0
            while True:
                rows = await asyncio.to_thread(
                    db.query,
                    "SELECT id, type, summary, created_at FROM events_log WHERE id > %s ORDER BY id",
                    (last_id,),
                )
                for r in rows:
                    last_id = r["id"]
                    yield f"data: {json.dumps(_jsonable(r))}\n\n"
                await asyncio.sleep(1.5)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
