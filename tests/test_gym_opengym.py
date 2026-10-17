"""Read-only openGym contract, mapped from synthetic gym data."""

from datetime import date, datetime, timezone
import json
from urllib.parse import urljoin

from fastapi.testclient import TestClient
import pytest

import config
from core import db
from domains.gym import plans
import web.gym_app as gym_app
from web import gym_og_data


@pytest.fixture
def og_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(gym_app, "GYM_OG_DIST_DIR", tmp_path / "dist")
    return TestClient(gym_app.create_gym_app(), follow_redirects=False)


def sign_in(client):
    response = client.post("/auth/session", json={"token": "test-token"})
    assert response.status_code == 200


def test_snapshot_maps_history_plan_timed_sets_warmups_and_weight():
    state = gym_og_data.map_snapshot(
        workouts=[{
            "id": 12,
            "date": date(2026, 10, 16),
            "title": "Upper A · Day 1 · Week 2 · Synthetic Plan",
            "created_at": datetime(2026, 10, 16, 9, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 10, 16, 10, tzinfo=timezone.utc),
        }],
        workout_sets=[
            {"workout_id": 12, "exercise_id": 5, "set_index": 1,
             "weight_kg": 20, "reps": 5, "duration_s": None, "is_warmup": True},
            {"workout_id": 12, "exercise_id": 8, "set_index": 1,
             "weight_kg": None, "reps": None, "duration_s": 45, "is_warmup": False},
            {"workout_id": 12, "exercise_id": 5, "set_index": 2,
             "weight_kg": 40, "reps": 8, "duration_s": None, "is_warmup": False},
        ],
        exercises=[
            {"id": 5, "name": "Synthetic Bench", "alias": "Synthetic Press",
             "muscle": "chest", "equipment": "barbell", "category": "strength", "unit": "reps"},
            {"id": 8, "name": "Synthetic Hold", "alias": "Synthetic Hold",
             "muscle": "waist", "equipment": None, "category": "strength", "unit": "min"},
            {"id": 13, "name": "Synthetic Bike", "alias": "Synthetic Bike",
             "muscle": "cardio", "equipment": "bike", "category": "cardio", "unit": "min"},
        ],
        active_plan={
            "days": [{
                "id": 9, "name": "Upper A", "exercises": [{
                    "exercise_id": 5, "target_sets": 3, "rep_min": 8, "rep_max": 12,
                }, {
                    "exercise_id": 8, "target_sets": 2, "rep_min": 45, "rep_max": 60,
                    "target_unit": "seconds",
                }],
            }, {"id": 10, "name": "Lower A", "exercises": []}],
        },
        next_day={"id": 10, "name": "Lower A"},
        bodyweight=[{"date": date(2026, 10, 15), "weight_kg": 71.5}],
        rest_sec=75,
        today=date(2026, 10, 17),
    )

    workout = state["workouts"][0]
    assert workout["id"] == "w-12"
    assert workout["name"] == "Upper A"
    assert workout["routineId"] == "r-9"
    assert workout["start"] == 1792141200000
    assert workout["end"] == 1792144800000
    assert workout["vol"] == 320
    assert workout["entries"] == [
        {"id": "m-5", "sets": [
            {"w": 20, "r": 5, "done": True, "warmup": True},
            {"w": 40, "r": 8, "done": True},
        ]},
        {"id": "m-8", "sets": [{"w": None, "r": None, "done": True, "sec": 45}]},
    ]
    assert state["routines"] == [{
        "id": "r-9", "name": "Upper A", "emoji": "",
        "ex": [
            {"id": "m-5", "sets": 3, "reps": 12, "repsMin": 8},
            {"id": "m-8", "sets": 2, "mode": "time", "sec": 60},
        ],
    }, {"id": "r-10", "name": "Lower A", "emoji": "", "ex": []}]
    assert state["dayPlan"] == {"2026-10-17": "r-10"}
    assert state["week"] == {}
    assert state["bodyweight"] == [{"d": "2026-10-15", "w": 71.5}]
    assert state["customEx"] == [
        {"id": "m-5", "n": "Synthetic Press", "bp": "chest", "tg": "chest", "eq": "barbell", "cat": "strength", "custom": True},
        {"id": "m-8", "n": "Synthetic Hold", "bp": "waist", "tg": "waist", "eq": "", "cat": "strength", "custom": True},
        {"id": "m-13", "n": "Synthetic Bike", "bp": "cardio", "tg": "cardio", "eq": "bike", "cat": "cardio", "custom": True},
    ]
    assert state["unit"] == "kg"
    assert state["restSec"] == 75
    assert "active" not in state


def test_empty_snapshot_contains_valid_empty_frontend_state():
    state = gym_og_data.map_snapshot(
        workouts=[], workout_sets=[], exercises=[], active_plan=None, next_day=None,
        bodyweight=[], rest_sec=180, today=date(2026, 10, 17),
    )

    assert state["workouts"] == []
    assert state["routines"] == []
    assert state["dayPlan"] == {}
    assert state["week"] == {}
    assert state["bodyweight"] == []
    assert state["customEx"] == []
    assert state["unit"] == "kg"
    assert state["restSec"] == 180


def test_revision_migration_covers_every_table_used_by_the_snapshot():
    sql = "\n".join(db.MIGRATIONS).lower()

    assert "create table if not exists gym_sync_state" in sql
    for table in (
        "workouts", "workout_sets", "gym_plans", "gym_plan_days", "gym_plan_exercises",
        "gym_workout_exercises", "exercises", "exercise_aliases", "body_measurements", "health_data",
    ):
        assert f"create trigger gym_sync_revision_{table}" in sql
    assert "after update of weight on health_data for each row when (old.weight is distinct from new.weight)" in sql
    assert "after insert on health_data for each row when (new.weight is not null)" in sql
    assert "after delete on health_data for each row when (old.weight is not null)" in sql
    assert "after update on workouts for each row when (old.* is distinct from new.*)" in sql


def test_open_gym_requires_session_cookie_and_returns_to_login(og_client):
    response = og_client.get("/og/")
    data = og_client.get("/og/api/data")

    assert response.status_code == 307
    assert response.headers["location"] == "/?next=%2Fog%2F"
    assert "openGym" not in response.text
    assert data.status_code == 401


def test_authenticated_open_gym_api_contract_and_read_only_put(og_client, monkeypatch):
    sign_in(og_client)
    monkeypatch.setattr(gym_og_data, "get_state", lambda: {"workouts": [], "customEx": []})
    monkeypatch.setattr(gym_og_data, "get_revision", lambda: {"rev": 9, "wid": "w9"})

    assert og_client.get("/og/api/me").json() == {"user": {"id": 1, "name": "Timo", "admin": False}}
    assert og_client.get("/og/api/config").json() == {
        "invite_only": True, "allow_guest": False, "password_login": False,
        "passkeys": False, "registration": False, "push": False,
        "media": False, "coach": False,
    }
    assert og_client.get("/og/api/health").json() == {"ok": True}
    assert og_client.get("/og/api/data").json() == {
        "state": {"workouts": [], "customEx": []}, "rev": 9,
    }
    assert og_client.get("/og/api/data/rev").json() == {"rev": 9, "wid": "w9"}

    response = og_client.put("/og/api/data", json={"state": {"workouts": []}})
    assert response.status_code == 423
    assert response.json() == {"error": "read_only"}


def test_open_gym_logout_deletes_the_existing_gym_session(og_client):
    sign_in(og_client)

    response = og_client.post("/og/api/logout")

    assert response.status_code == 204
    assert 'mantis_gym="";' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert og_client.get("/og/api/data").status_code == 401


def test_open_gym_static_gates_stay_closed_without_dashboard_token(og_client, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>OG test</title>", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('install', () => {})", encoding="utf-8")
    monkeypatch.setattr(gym_app, "GYM_OG_DIST_DIR", dist)
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "")
    forged, _ = gym_app._session_value()
    og_client.cookies.set(gym_app.COOKIE, forged)

    assert og_client.get("/og/").status_code == 503
    assert og_client.get("/og/sw.js").status_code == 503
    assert og_client.get("/og/api/data").status_code == 503


def test_open_gym_snapshot_retries_when_revision_moves_while_state_is_read(og_client, monkeypatch):
    sign_in(og_client)
    revisions = iter([
        {"rev": 1, "wid": "old"}, {"rev": 2, "wid": "new"},
        {"rev": 2, "wid": "new"}, {"rev": 2, "wid": "new"},
    ])
    states = iter([{"workouts": [{"id": "old"}]}, {"workouts": [{"id": "new"}]}])
    monkeypatch.setattr(gym_og_data, "get_revision", lambda: next(revisions))
    monkeypatch.setattr(gym_og_data, "get_state", lambda: next(states))

    response = og_client.get("/og/api/data")

    assert response.status_code == 200
    assert response.json() == {"state": {"workouts": [{"id": "new"}]}, "rev": 2}


def test_bodyweight_keeps_the_newest_plausible_measurement_per_day():
    state = gym_og_data.map_snapshot(
        workouts=[], workout_sets=[], exercises=[], active_plan=None, next_day=None,
        bodyweight=[
            {"date": date(2026, 7, 1), "weight_kg": 72.0,
             "recorded_at": datetime(2026, 7, 1, 8, tzinfo=timezone.utc), "source_order": 0},
            {"date": date(2026, 7, 1), "weight_kg": 71.5,
             "recorded_at": datetime(2026, 7, 1, 12, tzinfo=timezone.utc), "source_order": 1},
            {"date": date(2026, 7, 2), "weight_kg": 700.0,
             "recorded_at": datetime(2026, 7, 2, 12, tzinfo=timezone.utc), "source_order": 1},
        ],
        rest_sec=90, today=date(2026, 7, 3),
    )

    assert state["bodyweight"] == [{"d": "2026-07-01", "w": 71.5}]


def test_foreign_origin_is_rejected_for_open_gym_write(og_client):
    sign_in(og_client)
    response = og_client.put(
        "/og/api/data", json={}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json() == {"error": "Origin not allowed"}


@pytest.mark.parametrize("method,path", [
    ("get", "/og/api/push/status"),
    ("post", "/og/api/media/missing"),
    ("post", "/og/api/coach/chat"),
    ("post", "/og/api/activity"),
])
def test_unsupported_open_gym_endpoints_are_json_404(og_client, method, path):
    sign_in(og_client)
    response = og_client.request(method.upper(), path, json={} if method != "get" else None)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_open_gym_static_files_are_authenticated_and_missing_dist_falls_back(og_client, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>OG test</title>", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('install', () => {})", encoding="utf-8")
    (dist / "manifest.json").write_text(
        json.dumps({"start_url": "./", "scope": "./"}), encoding="utf-8"
    )
    monkeypatch.setattr(gym_app, "GYM_OG_DIST_DIR", dist)

    assert og_client.get("/og/").headers["location"] == "/?next=%2Fog%2F"
    assert og_client.get("/og/assets/main.js").headers["location"] == "/?next=%2Fog%2F"
    sign_in(og_client)
    assert "OG test" in og_client.get("/og/").text
    worker = og_client.get("/og/sw.js")
    assert worker.headers["service-worker-allowed"] == "/og/"
    manifest = og_client.get("/og/manifest.json").json()
    assert urljoin("http://testserver/og/manifest.json", manifest["scope"]) == "http://testserver/og/"
    assert urljoin("http://testserver/og/manifest.json", manifest["start_url"]) == "http://testserver/og/"
    assert og_client.get("/og/../index.html").status_code == 404

    monkeypatch.setattr(gym_app, "GYM_OG_DIST_DIR", tmp_path / "missing")
    missing = og_client.get("/og/")
    assert missing.status_code == 307
    assert missing.headers["location"] == "/"


@pytest.mark.db
def test_synthetic_database_rows_map_and_advance_gym_revision():
    """Use only synthetic rows in mantis_test; this never inserts real health data."""
    from uuid import uuid4

    try:
        db.query_one("SELECT 1 AS connected")
    except Exception:
        pytest.skip("mantis_test/PostgreSQL is not reachable")

    db.run_migrations()
    try:
        previous = db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"]
    except Exception:
        pytest.skip("mantis_test does not have the gym sync migration")

    token = uuid4().hex
    plan_name = f"OG synthetic {token}"
    alias = f"OG press alias {token}"
    day_name = f"OG Upper {token}"
    exercise_id = workout_id = plan_id = day_id = None
    try:
        exercise_id = db.insert_returning(
            """INSERT INTO exercises (name,category,muscle,unit,equipment)
               VALUES (%s,'strength','chest','reps','barbell') RETURNING id""",
            (f"OG press {token}",),
        )
        db.execute(
            """INSERT INTO exercise_aliases (alias,normalized_alias,exercise_id)
               VALUES (%s,%s,%s)""",
            (alias, alias.casefold(), exercise_id),
        )
        plan_id = db.insert_returning(
            """INSERT INTO gym_plans (name,start_date,active)
               VALUES (%s,CURRENT_DATE,FALSE) RETURNING id""",
            (plan_name,),
        )
        day_id = db.insert_returning(
            """INSERT INTO gym_plan_days (plan_id,name,sort_order)
               VALUES (%s,%s,0) RETURNING id""",
            (plan_id, day_name),
        )
        db.execute(
            """INSERT INTO gym_plan_exercises
               (day_id,exercise_id,sort_order,target_sets,rep_min,rep_max,rest_s)
               VALUES (%s,%s,0,3,8,12,75)""",
            (day_id, exercise_id),
        )
        workout_id = db.insert_returning(
            """INSERT INTO workouts (date,title,type,created_at,ended_at)
               VALUES (CURRENT_DATE,%s,'gym',NOW(),NOW()) RETURNING id""",
            (f"{day_name} · Day 1 · Week 1 · {plan_name}",),
        )
        db.execute(
            """INSERT INTO workout_sets
               (workout_id,exercise_id,set_index,reps,weight_kg,is_warmup)
               VALUES (%s,%s,1,5,20,TRUE),(%s,%s,2,8,40,FALSE)""",
            (workout_id, exercise_id, workout_id, exercise_id),
        )

        exercises = db.query(
            """SELECT e.id,e.name,e.muscle,e.equipment,e.category,e.unit,e.dumbbell_pair,
                      COALESCE(alias.alias,e.name) AS alias
               FROM exercises e LEFT JOIN LATERAL (
                   SELECT ea.alias FROM exercise_aliases ea WHERE ea.exercise_id=e.id
                     AND LOWER(ea.alias)<>LOWER(e.name) ORDER BY ea.id LIMIT 1
               ) alias ON TRUE WHERE e.id=%s""",
            (exercise_id,),
        )
        workouts = db.query(
            "SELECT id,date,title,created_at,ended_at FROM workouts WHERE id=%s",
            (workout_id,),
        )
        workout_sets = db.query(
            """SELECT workout_id,exercise_id,set_index,weight_kg,reps,duration_s,
                      COALESCE(is_warmup,FALSE) AS is_warmup FROM workout_sets
               WHERE workout_id=%s ORDER BY set_index,id""",
            (workout_id,),
        )
        active_plan = plans.get_plan(plan_id)
        state = gym_og_data.map_snapshot(
            workouts=workouts,
            workout_sets=workout_sets,
            exercises=exercises,
            active_plan=active_plan,
            next_day=active_plan["days"][0],
            bodyweight=[],
            rest_sec=75,
            today=date.today(),
        )
        assert state["customEx"][0]["n"] == alias
        assert state["routines"][0]["ex"][0] == {
            "id": f"m-{exercise_id}", "sets": 3, "reps": 12, "repsMin": 8,
        }
        assert state["workouts"][0]["entries"][0]["sets"] == [
            {"w": 20, "r": 5, "done": True, "warmup": True},
            {"w": 40, "r": 8, "done": True},
        ]
        no_op_rev = db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"]
        db.execute("UPDATE workouts SET title=title WHERE id=%s", (workout_id,))
        assert db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"] == no_op_rev
        current = db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"]
        assert current > previous
    finally:
        if workout_id is not None:
            db.execute("DELETE FROM workouts WHERE id=%s", (workout_id,))
        if plan_id is not None:
            db.execute("DELETE FROM gym_plans WHERE id=%s", (plan_id,))
        if exercise_id is not None:
            db.execute("DELETE FROM exercises WHERE id=%s", (exercise_id,))


@pytest.mark.parametrize(("pair", "expected_volume", "expects_each"), [
    (None, 320, True), (True, 320, True), (False, 160, False),
])
def test_dumbbell_volume_matches_mantis_pair_contract(pair, expected_volume, expects_each):
    state = gym_og_data.map_snapshot(
        workouts=[{
            "id": 7, "date": date(2026, 10, 17), "title": "Dumbbell Press",
            "created_at": datetime(2026, 10, 17, 9, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 10, 17, 10, tzinfo=timezone.utc),
        }],
        workout_sets=[{
            "workout_id": 7, "exercise_id": 5, "set_index": 1,
            "weight_kg": 20, "reps": 8, "duration_s": None, "is_warmup": False,
        }],
        exercises=[{
            "id": 5, "name": "Dumbbell Press", "equipment": "dumbbell", "dumbbell_pair": pair,
        }],
        active_plan=None, next_day=None, bodyweight=[], rest_sec=90,
        today=date(2026, 10, 17),
    )

    workout = state["workouts"][0]
    assert workout["vol"] == expected_volume
    target = workout["entries"][0].get("target")
    assert (target == {"dbLoad": "each", "mantisPair": True}) is expects_each


@pytest.mark.db
def test_health_data_revision_ignores_non_weight_and_noop_updates():
    """Only a changed weight value advances openGym's bodyweight revision."""
    from datetime import timedelta

    try:
        db.query_one("SELECT 1 AS connected")
    except Exception:
        pytest.skip("mantis_test/PostgreSQL is not reachable")
    db.run_migrations()

    measured_on = next(
        date.today() + timedelta(days=offset)
        for offset in range(1000, 20000)
        if db.query_one(
            "SELECT 1 FROM health_data WHERE date=%s", (date.today() + timedelta(days=offset),)
        ) is None
    )
    try:
        db.execute(
            "INSERT INTO health_data (date,steps,weight,updated_at) VALUES (%s,%s,NULL,NOW())",
            (measured_on, 1),
        )
        rev = db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"]
        db.execute("UPDATE health_data SET steps=steps+1 WHERE date=%s", (measured_on,))
        assert db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"] == rev
        db.execute("UPDATE health_data SET steps=steps WHERE date=%s", (measured_on,))
        assert db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"] == rev

        db.execute("UPDATE health_data SET weight=71.5 WHERE date=%s", (measured_on,))
        rev += 1
        assert db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"] == rev
        db.execute("UPDATE health_data SET weight=weight WHERE date=%s", (measured_on,))
        assert db.query_one("SELECT rev FROM gym_sync_state WHERE id=1")["rev"] == rev
    finally:
        db.execute("DELETE FROM health_data WHERE date=%s", (measured_on,))

