"""
Mantis Dashboard API – läuft IM Mantis-Prozess (geteilter State mit dem Agent).
Voll interaktiv: REST für alle Domänen + 2-Wege-Chat (SSE-Streaming) + Live-Feeds.

App-Factory: `/health` bleibt hier, alle übrigen Endpoints liegen in
`web/routers/<domain>.py`. Jedes Router-Modul exportiert `build_router(orch)`
und wird unten via `app.include_router(...)` eingebunden.
"""
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
from core import db

log = logging.getLogger("mantis.api")


def create_app(orch=None) -> FastAPI:
    app = FastAPI(title="Mantis Dashboard", docs_url=None, redoc_url=None)

    from web.auth import DashboardAuth, origins, COOKIE, SESSION_SECONDS, session_value
    app.add_middleware(DashboardAuth)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins(),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Mantis-Token"],
    )

    @app.post("/auth/session")
    async def login(request: Request):
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, session_value(), max_age=SESSION_SECONDS,
                            httponly=True, secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.delete("/auth/session")
    async def logout():
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE)
        return response

    # ── System Health-Check ──────────────────────────────────────────────────
    @app.get("/health")
    async def system_health():
        """Schnell-Check: DB, Ollama, Telegram — für Monitoring und Aufwach-Diagnose."""
        import httpx as _httpx
        checks = {}

        # DB
        try:
            db.query_one("SELECT 1")
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {e}"

        # Ollama
        try:
            async with _httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{config.OLLAMA_BASE_URL}/api/tags")
            checks["ollama"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as e:
            checks["ollama"] = f"error: {e}"

        # Telegram (nur prüfen ob Token gesetzt)
        checks["telegram"] = "ok" if config.TELEGRAM_BOT_TOKEN else "no token"

        # Orchestrator
        checks["orchestrator"] = "ok" if orch is not None else "not attached"

        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)

    # ── Domänen-Router einbinden ──────────────────────────────────────────────
    from web import routers
    for module in routers.ROUTER_MODULES:
        app.include_router(module.build_router(orch))

    return app
