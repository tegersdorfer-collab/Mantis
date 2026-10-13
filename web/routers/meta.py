"""
Meta — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response



log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(
            (WEB_DIR / "index.html").read_text(),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    @router.get("/usage_costs.js")
    def usage_costs_script():
        return Response(
            (WEB_DIR / "usage_costs.js").read_text(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/mcp/tools")
    def mcp_tools_list():
        from web.mcp_server import MCP_TOOLS
        return {"tools": MCP_TOOLS}

    @router.post("/mcp/call")
    async def mcp_call(req: Request):
        from web.mcp_server import _handle_mcp_call
        body = await req.json()
        result = await asyncio.to_thread(_handle_mcp_call, body.get("tool", ""), body.get("args", {}))
        return {"result": result}

    @router.get("/api/eval/cases")
    def eval_cases():
        from core.eval_suite import EVAL_CASES
        return [{"name": c.name, "description": c.description, "prompt": c.prompt}
                for c in EVAL_CASES]

    @router.post("/api/eval/run")
    async def eval_run():
        if not orch:
            return JSONResponse({"error": "Orchestrator nicht verfügbar"}, status_code=503)
        from core.eval_suite import EvalRunner
        runner = EvalRunner(orch)
        await runner.run_all()
        return {"results": runner.to_dict(), "summary": runner.summary()}

    @router.get("/api/skills/procedures")
    def list_skill_procedures():
        from core.skill_md import list_all
        return list_all()

    @router.get("/api/skills/procedures/{name}")
    def get_skill_procedure(name: str):
        from core.skill_md import get_skill
        s = get_skill(name)
        if not s:
            return JSONResponse({"error": "nicht gefunden"}, status_code=404)
        return s

    @router.delete("/api/skills/procedures/{name}")
    def delete_skill_procedure(name: str):
        from core.skill_md import delete_skill
        if delete_skill(name):
            return {"ok": True}
        return JSONResponse({"error": "nicht gefunden"}, status_code=404)

    @router.get("/sw.js")
    def service_worker():
        from fastapi.responses import Response
        return Response(
            (WEB_DIR / "sw.js").read_text(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/manifest.json")
    def manifest():
        return JSONResponse({
            "name": "Mantis", "short_name": "Mantis",
            "start_url": "/", "display": "standalone",
            "background_color": "#0a0f1e", "theme_color": "#0a0f1e",
            "icons": [{"src": "https://em-content.zobj.net/source/apple/391/robot_1f916.png",
                       "sizes": "160x160", "type": "image/png"}],
        })

    return router
