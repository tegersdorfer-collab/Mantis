"""Eigenständige, statische Gym-Tracker-App mit eigener HTTP-Session."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from pathlib import Path
import time

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from domains.gym import tracker


COOKIE = "mantis_gym"
STATIC_DIR = Path(__file__).parent / "gym"
STATIC_FILES = {
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.png": ("icon.png", "image/png"),
}


class LoginBody(BaseModel):
    token: str


class SetBody(BaseModel):
    client_id: str
    exercise: str
    weight_kg: float
    reps: int


class SetPatch(BaseModel):
    weight_kg: float | None = None
    reps: int | None = None


def _token_ok(value: str) -> bool:
    expected = config.DASHBOARD_TOKEN
    return bool(expected) and isinstance(value, str) and hmac.compare_digest(
        value.encode(), expected.encode()
    )


def _session_value() -> tuple[str, int]:
    expiry = int(time.time()) + config.GYM_SESSION_DAYS * 86400
    expiry_text = str(expiry)
    signature = hmac.new(
        config.DASHBOARD_TOKEN.encode(),
        ("gym-session:" + expiry_text).encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{expiry_text}.{signature}", config.GYM_SESSION_DAYS * 86400


def _session_ok(value: str | None) -> bool:
    try:
        expiry, signature = value.split(".")
        if int(expiry) <= time.time():
            return False
        expected = hmac.new(
            config.DASHBOARD_TOKEN.encode(),
            ("gym-session:" + expiry).encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (AttributeError, TypeError, ValueError):
        return False


async def _require_auth(request: Request) -> None:
    if not config.DASHBOARD_TOKEN:
        raise HTTPException(status_code=503, detail="Set DASHBOARD_TOKEN on the server")
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not (_token_ok(bearer) or _session_ok(request.cookies.get(COOKIE))):
        raise HTTPException(status_code=401, detail="Authentication required")


def _static_response(path: str) -> FileResponse:
    filename, media_type = STATIC_FILES[path]
    file_path = STATIC_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


def create_gym_app() -> FastAPI:
    app = FastAPI(title="Mantis Gym Tracker", docs_url=None, redoc_url=None)
    api = APIRouter(prefix="/api", dependencies=[Depends(_require_auth)])

    @app.middleware("http")
    async def check_write_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            own_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin != own_origin:
                return JSONResponse(
                    {"error": "Origin not allowed"},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
        return await call_next(request)

    @app.exception_handler(tracker.TrackerError)
    async def tracker_error_handler(_request: Request, error: tracker.TrackerError):
        return JSONResponse({"error": error.message}, status_code=error.status)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/app.js", include_in_schema=False)
    async def app_js():
        return _static_response("/app.js")

    @app.get("/app.css", include_in_schema=False)
    async def app_css():
        return _static_response("/app.css")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return _static_response("/manifest.webmanifest")

    @app.get("/icon.png", include_in_schema=False)
    async def icon():
        return _static_response("/icon.png")

    @app.post("/auth/session")
    async def login(body: LoginBody, request: Request):
        if not config.DASHBOARD_TOKEN:
            raise HTTPException(status_code=503, detail="Set DASHBOARD_TOKEN on the server")
        if not _token_ok(body.token):
            raise HTTPException(status_code=401, detail="Invalid token")
        value, max_age = _session_value()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            value,
            max_age=max_age,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.delete("/auth/session", status_code=204)
    async def logout(request: Request):
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @api.get("/state")
    async def state():
        return await asyncio.to_thread(tracker.get_state)

    @api.post("/sets")
    async def add_set(body: SetBody):
        return await asyncio.to_thread(
            tracker.log_set,
            body.client_id,
            body.exercise,
            body.weight_kg,
            body.reps,
        )

    @api.patch("/sets/{set_id}")
    async def update_set(set_id: int, body: SetPatch):
        values = body.model_dump(exclude_unset=True)
        return await asyncio.to_thread(
            tracker.update_set,
            set_id,
            values.get("weight_kg"),
            values.get("reps"),
        )

    @api.delete("/sets/{set_id}", status_code=204)
    async def delete_set(set_id: int):
        await asyncio.to_thread(tracker.delete_set, set_id)
        return Response(status_code=204)

    @api.post("/finish")
    async def finish():
        return await asyncio.to_thread(tracker.finish_session)

    @api.get("/history")
    async def history(exercise: str, limit: int = 5):
        return await asyncio.to_thread(tracker.history, exercise, limit)

    app.include_router(api)
    return app
