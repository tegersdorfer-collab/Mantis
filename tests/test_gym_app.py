"""HTTP-Vertrag und Authentifizierung der eigenständigen Gym-App."""

from datetime import date

from fastapi.testclient import TestClient
import pytest

import config
from domains.gym import library, overview, plans, tracker
from web.gym_app import create_gym_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "t")
    monkeypatch.setattr(
        tracker,
        "get_state",
        lambda: {"session": None, "exercises": []},
    )
    monkeypatch.setattr(
        plans,
        "home_state",
        lambda: {"active_plan": None, "week_number": None, "week_days": [], "next_day": None},
    )
    return TestClient(create_gym_app())


@pytest.fixture
def logged_in(client):
    response = client.post("/auth/session", json={"token": "t"})
    assert response.status_code == 200
    return client


def test_shell_and_manifest_are_public_but_api_requires_auth(client):
    shell = client.get("/")
    assert shell.status_code == 200
    assert "text/html" in shell.headers["content-type"]
    assert shell.headers["cache-control"] == "no-cache"
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/api/state").status_code == 401


def test_login_checks_token_and_sets_a_long_lived_http_only_cookie(client):
    assert client.post("/auth/session", json={"token": "wrong"}).status_code == 401
    response = client.post("/auth/session", json={"token": "t"})

    assert response.status_code == 200
    assert "mantis_gym" in response.cookies
    assert "Max-Age=15552000" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/state").status_code == 200


def test_login_returns_503_when_server_token_is_unset(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "")

    assert client.post("/auth/session", json={"token": ""}).status_code == 503


def test_foreign_origin_is_rejected_on_write(logged_in):
    response = logged_in.post(
        "/api/sets",
        json={
            "client_id": "synthetic-client",
            "exercise": "Squats",
            "weight_kg": 20,
            "reps": 5,
        },
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403


def test_tracker_error_maps_to_its_http_status(logged_in, monkeypatch):
    def raise_closed(*_args, **_kwargs):
        raise tracker.TrackerError(409, "geschlossen")

    monkeypatch.setattr(tracker, "delete_set", raise_closed)
    response = logged_in.delete("/api/sets/5")

    assert response.status_code == 409
    assert response.json() == {"error": "geschlossen"}


def test_api_also_accepts_a_bearer_token(client):
    response = client.get("/api/state", headers={"Authorization": "Bearer t"})

    assert response.status_code == 200


def test_v2_routes_remain_authenticated_and_reject_a_foreign_write_origin(logged_in):
    unauthenticated = TestClient(create_gym_app())
    assert unauthenticated.get("/api/plans").status_code == 401
    assert unauthenticated.get("/api/exercises").status_code == 401
    assert unauthenticated.get("/api/workouts").status_code == 401
    response = logged_in.post(
        "/api/sessions/start",
        json={"day_id": 1},
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403


def test_overview_is_authenticated_and_exposes_chart_and_profile_data(client, logged_in, monkeypatch):
    data = {
        "workout_count": 12,
        "streak_weeks": 3,
        "weekly": [{"week_start": "2035-06-11", "workout_count": 2, "volume_kg": 2400}],
        "latest_measurement": {"date": "2035-06-12", "weight_kg": 72.5, "body_fat": 18.0},
    }
    monkeypatch.setattr(overview, "dashboard", lambda weeks=4: data)
    unauthenticated = TestClient(create_gym_app())

    assert unauthenticated.get("/api/overview").status_code == 401
    response = logged_in.get("/api/overview?weeks=4")

    assert response.status_code == 200
    assert response.json() == data


def test_measurement_route_enforces_origin_and_validates_values(logged_in, monkeypatch):
    saved = {"date": "2035-06-12", "weight_kg": 72.5, "body_fat": 18.0}
    calls = []

    def save_measurement(weight_kg, body_fat, date_=None):
        calls.append((weight_kg, body_fat, date_))
        return saved

    monkeypatch.setattr(overview, "save_measurement", save_measurement)

    blocked = logged_in.post(
        "/api/measurements",
        json={"weight_kg": 72.5},
        headers={"Origin": "http://evil.example"},
    )
    accepted = logged_in.post(
        "/api/measurements",
        json={"weight_kg": 72.5, "body_fat": 18.0, "date": "2035-06-12"},
        headers={"Origin": "http://testserver"},
    )
    invalid = logged_in.post(
        "/api/measurements",
        json={"body_fat": 101},
        headers={"Origin": "http://testserver"},
    )

    assert blocked.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == saved
    assert calls == [(72.5, 18.0, date(2035, 6, 12))]
    assert invalid.status_code == 422


def test_exercise_instructions_route_requires_auth_and_returns_saved_text(client, logged_in, monkeypatch):
    saved = {"id": 9, "name": "Synthetic Row", "instructions": "Rücken neutral halten."}
    calls = []

    def update_instructions(exercise_id, instructions):
        calls.append((exercise_id, instructions))
        return saved

    monkeypatch.setattr(library, "update_instructions", update_instructions)
    unauthenticated = TestClient(create_gym_app())

    assert unauthenticated.patch(
        "/api/exercises/9", json={"instructions": "Rücken neutral halten."}
    ).status_code == 401
    response = logged_in.patch("/api/exercises/9", json={"instructions": "Rücken neutral halten."})

    assert response.status_code == 200
    assert response.json() == saved
    assert calls == [(9, "Rücken neutral halten.")]


def test_session_exercise_can_be_replaced_and_target_updated(logged_in, monkeypatch):
    saved = {"exercise_id": 44, "name": "Synthetic Cable Row", "rep_min": 8, "rep_max": 12}
    calls = []

    def update_session_exercise(workout_id, entry_id, **values):
        calls.append((workout_id, entry_id, values))
        return saved

    monkeypatch.setattr(plans, "update_session_exercise", update_session_exercise)
    response = logged_in.patch(
        "/api/sessions/5/exercises/7",
        json={"exercise_id": 44, "target_sets": 4, "rep_min": 8, "rep_max": 12},
    )

    assert response.status_code == 200
    assert response.json() == saved
    assert calls == [(5, 7, {"exercise_id": 44, "target_sets": 4, "rep_min": 8, "rep_max": 12})]


def test_session_exercise_remove_requires_auth_and_rejects_foreign_origin(logged_in, monkeypatch):
    removed = []
    monkeypatch.setattr(plans, "remove_session_exercise", lambda workout, entry: removed.append((workout, entry)))
    unauthenticated = TestClient(create_gym_app())

    assert unauthenticated.delete("/api/sessions/5/exercises/7").status_code == 401
    blocked = logged_in.delete(
        "/api/sessions/5/exercises/7", headers={"Origin": "http://evil.example"}
    )
    accepted = logged_in.delete(
        "/api/sessions/5/exercises/7", headers={"Origin": "http://testserver"}
    )

    assert blocked.status_code == 403
    assert accepted.status_code == 204
    assert removed == [(5, 7)]
