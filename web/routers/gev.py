"""Geschützter SSE-Kanal und Quittierung für God's Eye View."""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.gev_control import GEV_BUS


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/gev/stream")
    async def command_stream():
        queue = GEV_BUS.subscribe()

        async def events():
            try:
                yield 'data: {"ready":true}\n\n'
                while True:
                    try:
                        command = await asyncio.wait_for(queue.get(), 25)
                        yield f"data: {json.dumps(command)}\n\n"
                    except asyncio.TimeoutError:
                        yield 'data: {"keepalive":true}\n\n'
            finally:
                GEV_BUS.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.post("/api/gev/ack")
    async def command_ack(body: dict):
        command_id = body.get("id")
        result = body.get("result")
        if (not isinstance(command_id, str) or len(command_id) != 32
                or not isinstance(result, dict)
                or not isinstance(result.get("ok"), bool)):
            raise HTTPException(status_code=400, detail="Ungültige Quittierung")
        clean = {"ok": result["ok"]}
        if result["ok"] is False:
            clean["error"] = str(result.get("error", "Unbekannter Fehler"))[:200]
        if not GEV_BUS.ack(command_id, clean):
            raise HTTPException(status_code=404, detail="Befehl nicht gefunden")
        return {"ok": True}

    return router
