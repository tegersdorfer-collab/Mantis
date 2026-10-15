"""HTTP-Vertrag und Authentifizierung der eigenständigen Gym-App."""

from fastapi.testclient import TestClient
import pytest

import config
from domains.gym import plans, tracker
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
