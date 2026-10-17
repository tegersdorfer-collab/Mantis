"""Eigenständige, statische Gym-Tracker-App mit eigener HTTP-Session."""

from __future__ import annotations

import asyncio
from datetime import date as Date
import hashlib
import hmac
from pathlib import Path
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import config
from domains.gym import library, overview, plans, tracker
from web import gym_og_data


COOKIE = "mantis_gym"
STATIC_DIR = Path(__file__).parent / "gym"
GYM_OG_DIST_DIR = Path(__file__).parent / "gym_og" / "frontend" / "dist"
# Die alte Vanilla-App liegt unter /old/; ihre Dateien sind öffentlich, weil /login sie braucht.
STATIC_FILES = {
    "app.js": ("app.js", "text/javascript"),
    "app.css": ("app.css", "text/css"),
    "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "icon.png": ("icon.png", "image/png"),
}
LOGIN_PATH = "/login"


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


class PlanBody(BaseModel):
    name: str
    start_date: str | None = None


class PlanPatch(BaseModel):
    name: str | None = None
    start_date: str | None = None


class DayBody(BaseModel):
    name: str


class DayOrderBody(BaseModel):
    day_ids: list[int]


class ExerciseOrderBody(BaseModel):
    entry_ids: list[int]


class PlanExerciseBody(BaseModel):
    exercise_id: int
    target_sets: int = 3
    rep_min: int | None = None
    rep_max: int | None = None
    rest_s: int = plans.DEFAULT_REST_SECONDS


class PlanExercisePatch(BaseModel):
    sort_order: int | None = None
    target_sets: int | None = None
    rep_min: int | None = None
    rep_max: int | None = None
    rest_s: int | None = None


class ExerciseBody(BaseModel):
    name: str
    muscle: str | None = None
    equipment: str | None = None
    rep_min: int = 6
    rep_max: int = 10


class ExercisePatch(BaseModel):
    instructions: str = Field(max_length=3000)


class MeasurementBody(BaseModel):
    weight_kg: float | None = Field(default=None, ge=20, le=500)
    body_fat: float | None = Field(default=None, ge=0, le=100)
    date: Date | None = None


class SessionStartBody(BaseModel):
    day_id: int | None = None


class SessionExerciseBody(BaseModel):
    exercise_id: int


class SessionExercisePatch(BaseModel):
    sort_order: int | None = None
    note: str | None = None
    skipped: bool | None = None
    exercise_id: int | None = None
    target_sets: int | None = None
    rep_min: int | None = None
    rep_max: int | None = None
    rest_s: int | None = None


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
    if not config.DASHBOARD_TOKEN:
        return False
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


def _login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PATH}?{urlencode({'next': next_path})}", status_code=307)


def _require_og_session(request: Request) -> bool:
    if not config.DASHBOARD_TOKEN:
        raise HTTPException(status_code=503, detail="Set DASHBOARD_TOKEN on the server")
    return _session_ok(request.cookies.get(COOKIE))


def _clear_gym_session(request: Request) -> Response:
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


def _gym_og_file_response(asset_path: str) -> FileResponse:
    root = GYM_OG_DIST_DIR.resolve()
    relative = "index.html" if not asset_path else asset_path
    file_path = (root / relative).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    headers = {"Cache-Control": "no-cache"}
    if relative == "sw.js":
        headers["Service-Worker-Allowed"] = "/"
    return FileResponse(
        file_path,
        media_type="application/javascript" if relative == "sw.js" else None,
        headers=headers,
    )


def _static_response(path: str) -> FileResponse:
    if path not in STATIC_FILES:
        raise HTTPException(status_code=404, detail="Not found")
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
    og_api = APIRouter(prefix="/og/api", dependencies=[Depends(_require_auth)])

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

    def old_index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        if not _require_og_session(request):
            return _login_redirect("/")
        if not (GYM_OG_DIST_DIR / "index.html").is_file():
            return RedirectResponse("/old/", status_code=307)
        return _gym_og_file_response("index.html")

    @app.get(LOGIN_PATH, include_in_schema=False)
    async def login_page():
        # The old app's token login; app.js follows `next` afterwards (internal paths only).
        return old_index()

    @app.get("/old", include_in_schema=False)
    async def old_no_slash():
        return RedirectResponse("/old/", status_code=307)

    @app.get("/old/", include_in_schema=False)
    async def old_app(request: Request):
        if not _require_og_session(request):
            return _login_redirect("/old/")
        return old_index()

    @app.get("/old/{filename}", include_in_schema=False)
    async def old_static(filename: str):
        return _static_response(filename)

    @app.get("/og", include_in_schema=False)
    async def open_gym_legacy_root():
        return RedirectResponse("/", status_code=307)

    @og_api.get("/me")
    async def open_gym_me():
        return {"user": {"id": 1, "name": "Timo", "admin": False}}

    @og_api.get("/config")
    async def open_gym_config():
        return {
            "invite_only": True,
            "allow_guest": False,
            "password_login": False,
            "passkeys": False,
            "registration": False,
            "push": False,
            "media": False,
            "coach": False,
        }

    @og_api.get("/health")
    async def open_gym_health():
        return {"ok": True}

    @og_api.get("/data")
    async def open_gym_data():
        for _ in range(3):
            before = await asyncio.to_thread(gym_og_data.get_revision)
            state = await asyncio.to_thread(gym_og_data.get_state)
            after = await asyncio.to_thread(gym_og_data.get_revision)
            if (before.get("rev"), before.get("wid")) == (after.get("rev"), after.get("wid")):
                return {"state": state, "rev": after["rev"]}
        raise HTTPException(status_code=503, detail="Gym data changed while the snapshot was read")

    @og_api.get("/data/rev")
    async def open_gym_data_revision():
        return await asyncio.to_thread(gym_og_data.get_revision)

    @og_api.put("/data")
    async def open_gym_data_read_only():
        return JSONResponse({"error": "read_only"}, status_code=423)

    @og_api.api_route(
        "/{unsupported_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def unsupported_open_gym_api(unsupported_path: str):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.post("/og/api/logout", status_code=204, include_in_schema=False)
    async def open_gym_logout(request: Request):
        return _clear_gym_session(request)

    app.include_router(og_api)

    @app.get("/og/{_legacy_path:path}", include_in_schema=False)
    async def open_gym_legacy_path(_legacy_path: str):
        # Old /og/ bookmarks; openGym now lives at the root (its API stays under /og/api).
        return RedirectResponse("/", status_code=307)

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
        return _clear_gym_session(request)

    @api.get("/state")
    async def state():
        current = await asyncio.to_thread(tracker.get_state)
        if current.get("session"):
            current["exercises"] = await asyncio.to_thread(
                plans.session_exercises,
                int(current["session"]["id"]),
                current["session"].get("title"),
                current.get("exercises", []),
            )
        current["home"] = await asyncio.to_thread(plans.home_state)
        return current

    @api.get("/overview")
    async def overview_data(weeks: int = 4):
        return await asyncio.to_thread(overview.dashboard, weeks=weeks)

    @api.post("/measurements")
    async def save_measurement(body: MeasurementBody):
        return await asyncio.to_thread(
            overview.save_measurement,
            body.weight_kg,
            body.body_fat,
            date_=body.date,
        )

    @api.post("/sessions/start")
    async def start_session(body: SessionStartBody):
        if body.day_id is not None:
            return await asyncio.to_thread(plans.start_plan_session, body.day_id)
        return await asyncio.to_thread(plans.start_free_session)

    @api.post("/sessions/{workout_id}/exercises")
    async def add_session_exercise(workout_id: int, body: SessionExerciseBody):
        return await asyncio.to_thread(
            plans.add_session_exercise, workout_id, body.exercise_id
        )

    @api.patch("/sessions/{workout_id}/exercises/{entry_id}")
    async def update_session_exercise(
        workout_id: int, entry_id: int, body: SessionExercisePatch,
    ):
        return await asyncio.to_thread(
            plans.update_session_exercise,
            workout_id,
            entry_id,
            **body.model_dump(exclude_unset=True),
        )

    @api.delete("/sessions/{workout_id}/exercises/{entry_id}", status_code=204)
    async def remove_session_exercise(workout_id: int, entry_id: int):
        await asyncio.to_thread(plans.remove_session_exercise, workout_id, entry_id)
        return Response(status_code=204)

    @api.get("/plans")
    async def list_plans():
        return await asyncio.to_thread(plans.list_plans)

    @api.post("/plans")
    async def create_plan(body: PlanBody):
        return await asyncio.to_thread(plans.create_plan, body.name, body.start_date)

    @api.get("/plans/{plan_id}")
    async def get_plan(plan_id: int):
        return await asyncio.to_thread(plans.get_plan, plan_id)

    @api.patch("/plans/{plan_id}")
    async def update_plan(plan_id: int, body: PlanPatch):
        return await asyncio.to_thread(
            plans.update_plan, plan_id, **body.model_dump(exclude_unset=True)
        )

    @api.post("/plans/{plan_id}/activate")
    async def activate_plan(plan_id: int):
        return await asyncio.to_thread(plans.activate_plan, plan_id)

    @api.post("/plans/{plan_id}/days")
    async def add_plan_day(plan_id: int, body: DayBody):
        return await asyncio.to_thread(plans.add_day, plan_id, body.name)

    @api.put("/plans/{plan_id}/days/order")
    async def reorder_plan_days(plan_id: int, body: DayOrderBody):
        return await asyncio.to_thread(plans.reorder_days, plan_id, body.day_ids)

    @api.patch("/plan-days/{day_id}")
    async def update_plan_day(day_id: int, body: DayBody):
        return await asyncio.to_thread(plans.update_day, day_id, name=body.name)

    @api.delete("/plan-days/{day_id}", status_code=204)
    async def delete_plan_day(day_id: int):
        await asyncio.to_thread(plans.delete_day, day_id)
        return Response(status_code=204)

    @api.post("/plan-days/{day_id}/exercises")
    async def add_plan_exercise(day_id: int, body: PlanExerciseBody):
        return await asyncio.to_thread(
            plans.add_exercise,
            day_id,
            body.exercise_id,
            target_sets=body.target_sets,
            rep_min=body.rep_min,
            rep_max=body.rep_max,
            rest_s=body.rest_s,
        )

    @api.put("/plan-days/{day_id}/exercises/order")
    async def reorder_plan_exercises(day_id: int, body: ExerciseOrderBody):
        return await asyncio.to_thread(plans.reorder_exercises, day_id, body.entry_ids)

    @api.patch("/plan-exercises/{entry_id}")
    async def update_plan_exercise(entry_id: int, body: PlanExercisePatch):
        return await asyncio.to_thread(
            plans.update_plan_exercise,
            entry_id,
            **body.model_dump(exclude_unset=True),
        )

    @api.delete("/plan-exercises/{entry_id}", status_code=204)
    async def delete_plan_exercise(entry_id: int):
        await asyncio.to_thread(plans.delete_plan_exercise, entry_id)
        return Response(status_code=204)

    @api.get("/exercises")
    async def list_exercises(search: str = "", muscle: str | None = None):
        return await asyncio.to_thread(library.list_exercises, search, muscle)

    @api.post("/exercises")
    async def create_exercise(body: ExerciseBody):
        return await asyncio.to_thread(
            library.create_exercise,
            body.name,
            body.muscle,
            body.equipment,
            body.rep_min,
            body.rep_max,
        )

    @api.get("/exercises/{exercise_id}")
    async def exercise_detail(exercise_id: int):
        return await asyncio.to_thread(library.exercise_detail, exercise_id)

    @api.patch("/exercises/{exercise_id}")
    async def update_exercise(exercise_id: int, body: ExercisePatch):
        return await asyncio.to_thread(
            library.update_instructions, exercise_id, body.instructions
        )

    @api.get("/workouts")
    async def workout_list(limit: int = 50):
        return await asyncio.to_thread(library.workout_list, limit)

    @api.get("/workouts/{workout_id}")
    async def workout_detail(workout_id: int):
        return await asyncio.to_thread(library.workout_detail, workout_id)

    @api.patch("/workouts/{workout_id}/sets/{set_id}")
    async def update_history_set(workout_id: int, set_id: int, body: SetPatch):
        values = body.model_dump(exclude_unset=True)
        return await asyncio.to_thread(
            library.update_history_set,
            workout_id,
            set_id,
            values.get("weight_kg"),
            values.get("reps"),
        )

    @api.delete("/workouts/{workout_id}/sets/{set_id}", status_code=204)
    async def delete_history_set(workout_id: int, set_id: int):
        await asyncio.to_thread(library.delete_history_set, workout_id, set_id)
        return Response(status_code=204)

    @api.delete("/workouts/{workout_id}", status_code=204)
    async def delete_workout(workout_id: int):
        await asyncio.to_thread(library.delete_workout, workout_id)
        return Response(status_code=204)

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

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def open_gym_static(asset_path: str, request: Request):
        if not _require_og_session(request):
            return _login_redirect("/")
        if not (GYM_OG_DIST_DIR / "index.html").is_file():
            return RedirectResponse("/old/", status_code=307)
        return _gym_og_file_response(asset_path)

    return app
