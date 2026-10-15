"""Plan-Seed und Planmodell für den Gym-Tracker mit synthetischer Historie."""

from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from core import db
from domains.gym import ap_text_import, plans, session
from domains.gym.aliases import normalize_alias


def test_plan_start_uses_week_one_after_the_last_gap_over_21_days():
    workouts = [
        {"date": date(2026, 7, 10), "title": "Lower A · Week 1 · New plan"},
        {"date": date(2026, 8, 19), "title": "Lower A · Week 1 · New plan"},
        {"date": date(2026, 8, 23), "title": "Upper A · Week 1 · New plan"},
    ]

    assert plans.choose_plan_start_date(workouts) == date(2026, 8, 19)


def test_rotation_week_uses_ap_titles_and_advances_after_a_complete_cycle():
    days = ["Lower A", "Upper A", "Lower B", "Upper B"]
    workouts = [
        {"id": 1, "date": date(2026, 8, 19), "title": "Lower B · Day 3 · Week 1 · New plan"},
        {"id": 2, "date": date(2026, 8, 21), "title": "Upper B · Day 4 · Week 1 · New plan"},
        {"id": 3, "date": date(2026, 8, 22), "title": "Lower A · Day 1 · Week 2 · New plan"},
        {"id": 4, "date": date(2026, 10, 5), "title": "Upper A · Day 2 · Week 7 · New plan"},
        {"id": 5, "date": date(2026, 10, 8), "title": "Upper B · Day 4 · Week 7 · New plan"},
    ]

    assert plans.rotation_week_number(date(2026, 8, 19), days, workouts[:2]) == 2
    assert plans.rotation_week_number(date(2026, 8, 19), days, workouts[:3]) == 2
    assert plans.rotation_week_number(date(2026, 8, 19), days, workouts) == 8


def test_rotation_week_falls_back_to_day_order_without_ap_week_markers():
    days = ["Lower A", "Upper A", "Lower B", "Upper B"]
    workouts = [
        {"id": 1, "date": date(2026, 8, 19), "title": "Lower B · Day 3"},
        {"id": 2, "date": date(2026, 8, 21), "title": "Upper B · Day 4"},
        {"id": 3, "date": date(2026, 8, 22), "title": "Lower A · Day 1"},
        {"id": 4, "date": date(2026, 8, 24), "title": "Upper A · Day 2"},
    ]

    assert plans.rotation_week_number(date(2026, 8, 19), days, workouts[:2]) == 2
    assert plans.rotation_week_number(date(2026, 8, 19), days, workouts) == 2


def test_next_rotation_day_after_upper_b_is_lower_a():
    days = [
        {"id": 1, "name": "Lower A"}, {"id": 2, "name": "Upper A"},
        {"id": 3, "name": "Lower B"}, {"id": 4, "name": "Upper B"},
    ]
    workouts = [
        {"id": 1, "date": date(2026, 10, 5), "title": "Upper A · Day 2 · Week 7 · New plan"},
        {"id": 2, "date": date(2026, 10, 8), "title": "Upper B · Day 4 · Week 7 · New plan"},
    ]

    last_day, next_day = plans._rotation_next_day(days, workouts)

    assert last_day == "Upper B"
    assert next_day["name"] == "Lower A"


def test_session_exercise_state_keeps_the_exercise_id(monkeypatch):
    monkeypatch.setattr(plans, "_exercise_ids", lambda _exercise_id: [])
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(plans, "_last_sets", lambda *_args, **_kwargs: [])

    entry = plans._session_entry_state(
        {
            "exercise_id": 456, "name": "Synthetic Squat", "sort_order": 0,
            "target_sets": 3, "rep_min": 6, "rep_max": 10,
        },
        workout_id=123,
    )

    assert entry["exercise_id"] == 456


@pytest.fixture
def plan_database():
    try:
        db.query_one("SELECT 1 AS ready")
    except Exception as exc:
        pytest.skip(f"mantis_test nicht erreichbar: {type(exc).__name__}")
    db.run_migrations()
    if session.open_session():
        pytest.skip("mantis_test enthält bereits eine offene Gym-Session")

    name = f"Synthetic New plan {uuid4()}"
    previous_active = [row["id"] for row in db.query("SELECT id FROM gym_plans WHERE active=TRUE")]
    exercise_ids = {}
    for exercise_name, category, unit, low, high in (
        (f"{name} Canonical", "strength", "reps", 8, 12),
        (f"{name} Alias", "strength", "reps", 6, 10),
        (f"{name} Cardio", "cardio", "km", None, None),
    ):
        exercise_ids[exercise_name] = db.insert_returning(
            """INSERT INTO exercises (name,category,unit,rep_min,rep_max)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (exercise_name, category, unit, low, high),
        )
    db.execute(
        """INSERT INTO exercise_aliases (alias,normalized_alias,exercise_id)
           VALUES (%s,%s,%s) ON CONFLICT (normalized_alias) DO NOTHING""",
        (f"{name} Alias", normalize_alias(f"{name} Alias"), exercise_ids[f"{name} Canonical"]),
    )

    workout_ids = []

    def add_workout(day, week, workout_date, count, *, include_cardio=False):
        day_number = plans.PLAN_DAY_ORDER.index(day) + 1
        title = f"{day} · Day {day_number} · Week {week} · {name}"
        moment = datetime.combine(workout_date, datetime.min.time(), tzinfo=ZoneInfo("Europe/Berlin"))
        workout_id = db.insert_returning(
            """INSERT INTO workouts (date,title,type,created_at,last_activity_at,ended_at)
               VALUES (%s,%s,'gym',%s,%s,%s) RETURNING id""",
            (workout_date, title, moment, moment, moment),
        )
        workout_ids.append(workout_id)
        if count:
            for set_index in range(1, count + 1):
                # Ein Teil der Historie nutzt einen Aliasdatensatz, der auf die
                # kanonische Übung zeigt. Die Reihenfolge bleibt dabei erhalten.
                exercise_id = (
                    exercise_ids[f"{name} Alias"]
                    if set_index == 1 else exercise_ids[f"{name} Canonical"]
                )
                db.execute(
                    """INSERT INTO workout_sets
                         (workout_id,exercise_id,set_index,reps,weight_kg,is_warmup)
                       VALUES (%s,%s,%s,8,50,FALSE)""",
                    (workout_id, exercise_id, set_index),
                )
            if include_cardio:
                db.execute(
                    """INSERT INTO workout_sets
                         (workout_id,exercise_id,set_index,reps,weight_kg,distance_km)
                       VALUES (%s,%s,%s,0,NULL,5)""",
                    (workout_id, exercise_ids[f"{name} Cardio"], count + 1),
                )
        return workout_id

    add_workout("Lower B", 1, date(2026, 8, 19), 0)
    add_workout("Upper B", 1, date(2026, 8, 21), 0)
    add_workout("Lower A", 2, date(2026, 8, 22), 2)
    add_workout("Upper A", 2, date(2026, 8, 24), 0)
    add_workout("Lower A", 6, date(2026, 10, 1), 3)
    add_workout("Upper A", 7, date(2026, 10, 5), 1)
    add_workout("Upper B", 7, date(2026, 10, 8), 4, include_cardio=True)

    created_plans = []
    yield {
        "name": name,
        "exercise_ids": exercise_ids,
        "workouts": workout_ids,
        "active": previous_active,
        "created_plans": created_plans,
    }

    db.execute("DELETE FROM workouts WHERE id=ANY(%s)", (workout_ids,))
    db.execute("DELETE FROM gym_plans WHERE name=%s", (name,))
    if created_plans:
        db.execute("DELETE FROM gym_plans WHERE id=ANY(%s)", (created_plans,))
    db.execute("DELETE FROM exercise_aliases WHERE normalized_alias=%s", (normalize_alias(f"{name} Alias"),))
    db.execute("DELETE FROM exercises WHERE id=ANY(%s)", (list(exercise_ids.values()),))
    db.execute("UPDATE gym_plans SET active=FALSE")
    for plan_id in previous_active:
        db.execute("UPDATE gym_plans SET active=TRUE WHERE id=%s", (plan_id,))


@pytest.mark.db
def test_seed_is_idempotent_uses_latest_order_and_median_working_sets(plan_database):
    name = plan_database["name"]

    first = plans.seed_plan(name)
    second = plans.seed_plan(name)

    assert first["id"] == second["id"]
    assert first["start_date"] == "2026-08-19"
    assert first["active"] is True
    assert [day["name"] for day in first["days"]] == ["Lower A", "Upper A", "Lower B", "Upper B"]
    lower_a = first["days"][0]
    assert [item["exercise_id"] for item in lower_a["exercises"]] == [
        plan_database["exercise_ids"][f"{name} Canonical"],
    ]
    exercise = lower_a["exercises"][0]
    assert exercise["name"] == f"{name} Canonical"
    assert exercise["target_sets"] == 3
    assert (exercise["rep_min"], exercise["rep_max"]) == (8, 12)
    assert db.query_one("SELECT COUNT(*) AS n FROM gym_plans WHERE active=TRUE")["n"] == 1
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM gym_plan_days WHERE plan_id=%s", (first["id"],)
    )["n"] == 4
    home = plans.home_state(date(2026, 10, 8))
    assert home["week_number"] == 8
    assert home["next_day"]["name"] == "Lower A"
    thursday = next(day for day in home["week_days"] if day["date"] == "2026-10-08")
    assert thursday["today"] is True
    assert thursday["trained"] is True


@pytest.mark.db
def test_plan_crud_validates_ranges_and_switches_exactly_one_active_plan(plan_database):
    seeded = plans.seed_plan(plan_database["name"])
    new_plan = plans.create_plan(f"Synthetic copy {uuid4()}", date(2026, 10, 15))
    plan_database["created_plans"].append(new_plan["id"])
    new_day = plans.add_day(new_plan["id"], "Day 1")
    item = plans.add_exercise(
        new_day["id"],
        plan_database["exercise_ids"][f"{plan_database['name']} Canonical"],
        target_sets=4,
        rep_min=8,
        rep_max=12,
    )
    updated = plans.update_plan_exercise(item["id"], target_sets=5, sort_order=0)
    plans.activate_plan(new_plan["id"])

    assert updated["target_sets"] == 5
    assert plans.get_plan(new_plan["id"])["active"] is True
    assert plans.get_plan(seeded["id"])["active"] is False
    assert db.query_one("SELECT COUNT(*) AS n FROM gym_plans WHERE active=TRUE")["n"] == 1


@pytest.mark.db
def test_plan_session_snapshots_day_and_reorders_session_exercises(plan_database):
    plan = plans.create_plan(f"Synthetic session {uuid4()}", date(2026, 8, 19))
    plan_database["created_plans"].append(plan["id"])
    day = plans.add_day(plan["id"], "Lower A")
    canonical_id = plan_database["exercise_ids"][f"{plan_database['name']} Canonical"]
    alias_id = plan_database["exercise_ids"][f"{plan_database['name']} Alias"]
    plans.add_exercise(day["id"], canonical_id)
    plans.add_exercise(day["id"], alias_id)
    plans.activate_plan(plan["id"])

    started = plans.start_plan_session(
        day["id"], datetime(2026, 10, 15, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    )
    plan_database["workouts"].append(started["id"])
    assert started["title"] == f"Lower A · Day 1 · Week 1 · {plan['name']}"
    entries = plans.session_exercises(started["id"], started["title"])
    assert [item["exercise_id"] for item in entries] == [canonical_id, alias_id]

    plans.update_session_exercise(
        started["id"], entries[1]["session_exercise_id"], sort_order=0
    )

    reordered = plans.session_exercises(started["id"], started["title"])
    assert [item["exercise_id"] for item in reordered] == [alias_id, canonical_id]


def _export_text(name, rows):
    """Baut einen synthetischen AP-Export; rows = (Tag, Datum, [(Übung, Ziel)])."""
    lines = []
    for index, (day, day_date, exercises) in enumerate(rows):
        number = plans.PLAN_DAY_ORDER.index(day) + 1
        lines.append(f'"{day} · Day {number} · Week {7 - index} · {name}";"{day_date} 19:37 h";"1 hr"')
        for position, (label, target) in enumerate(exercises, 1):
            suffix = f" · {target} reps" if target else ""
            lines.extend([f'"{position}. {label}{suffix}"', "#;KG;REPS", "1;50;8"])
        lines.append("")
    return "\n".join(lines)


@pytest.mark.db
def test_seed_takes_rep_targets_from_latest_ap_export_workout_of_same_day(plan_database):
    name = plan_database["name"]
    export = _export_text(name, [
        ("Lower A", "2026-10-05", [(f"{name} Canonical", 5)]),
        ("Upper B", "2026-10-08", [(f"{name} Alias", 7)]),
        ("Upper B", "2026-10-02", [(f"{name} Alias", 9)]),
    ])
    workouts = ap_text_import.parse_export(export)

    preview = plans.preview_seed(name, ap_workouts=workouts)
    seeded = plans.seed_plan(name, ap_workouts=workouts)

    lower_a = preview["days"][0]["exercises"][0]
    upper_b = preview["days"][3]["exercises"][0]
    assert (lower_a["rep_min"], lower_a["rep_max"]) == (5, 5)
    assert (upper_b["rep_min"], upper_b["rep_max"]) == (7, 7)
    stored = seeded["days"][3]["exercises"][0]
    assert (stored["rep_min"], stored["rep_max"], stored["target_unit"]) == (7, 7, "reps")


@pytest.mark.db
def test_seed_without_ap_export_keeps_exercise_defaults(plan_database):
    seeded = plans.seed_plan(plan_database["name"])

    assert all(
        item["target_unit"] == "reps"
        for day in seeded["days"] for item in day["exercises"]
    )
    first = seeded["days"][0]["exercises"][0]
    assert (first["rep_min"], first["rep_max"]) == (8, 12)


@pytest.mark.db
def test_seed_includes_time_based_exercises_with_seconds_target(plan_database):
    name = plan_database["name"]
    hang_id = db.insert_returning(
        """INSERT INTO exercises (name,category,unit,rep_min,rep_max,load_mode)
           VALUES (%s,'strength','min',6,10,'added') RETURNING id""",
        (f"{name} Hang",),
    )
    started = None
    try:
        upper_b = db.query_one(
            "SELECT id FROM workouts WHERE title LIKE %s ORDER BY date DESC LIMIT 1", (f"Upper B%{name}",)
        )["id"]
        for index, seconds in ((5, 34), (6, 27)):
            db.execute(
                """INSERT INTO workout_sets
                     (workout_id,exercise_id,set_index,reps,weight_kg,duration_s,is_warmup)
                   VALUES (%s,%s,%s,NULL,20,%s,FALSE)""",
                (upper_b, hang_id, index, seconds),
            )

        seeded = plans.seed_plan(name)
        items = seeded["days"][3]["exercises"]
        hang = next(item for item in items if item["exercise_id"] == hang_id)

        assert hang["target_unit"] == "seconds"
        assert hang["target_sets"] == 2
        assert (hang["rep_min"], hang["rep_max"]) == (30, 40)
        assert [item["target_unit"] for item in items if item is not hang] == ["reps"]

        started = plans.start_plan_session(
            seeded["days"][3]["id"], datetime(2026, 10, 15, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        )
        plan_database["workouts"].append(started["id"])
        state = plans.session_exercises(started["id"], started["title"])
        entry = next(item for item in state if item["exercise_id"] == hang_id)
        assert entry["target_unit"] == "seconds"
        assert entry["prefill"] == [{"weight_kg": 20.0, "reps": 34}, {"weight_kg": 20.0, "reps": 27}]
    finally:
        if started:
            db.execute("DELETE FROM workouts WHERE id=%s", (started["id"],))
        db.execute("DELETE FROM gym_plans WHERE name=%s", (name,))
        db.execute("DELETE FROM workout_sets WHERE exercise_id=%s", (hang_id,))
        db.execute("DELETE FROM exercises WHERE id=%s", (hang_id,))


@pytest.mark.db
def test_seed_resolves_equipment_variants_to_the_same_ids_as_the_import(plan_database, monkeypatch):
    name = plan_database["name"]
    base = f"{name} Zugvariante"
    created = []
    try:
        barbell_id = db.insert_returning(
            """INSERT INTO exercises (name,category,unit,equipment,load_mode,rep_min,rep_max)
               VALUES (%s,'strength','reps','machine','external',6,10) RETURNING id""",
            (base,),
        )
        dumbbell_id = db.insert_returning(
            """INSERT INTO exercises (name,category,unit,equipment,load_mode,rep_min,rep_max)
               VALUES (%s,'strength','reps','dumbbell','external',6,10) RETURNING id""",
            (f"{base} (Dumbbell)",),
        )
        created.extend([barbell_id, dumbbell_id])
        upper_b = db.query_one(
            "SELECT id FROM workouts WHERE title LIKE %s ORDER BY date DESC LIMIT 1", (f"Upper B%{name}",)
        )["id"]
        for exercise_id, first_index in ((barbell_id, 10), (dumbbell_id, 20)):
            for offset in range(2):
                db.execute(
                    """INSERT INTO workout_sets (workout_id,exercise_id,set_index,reps,weight_kg,is_warmup)
                       VALUES (%s,%s,%s,8,20,FALSE)""",
                    (upper_b, exercise_id, first_index + offset),
                )
        export = _export_text(name, [
            ("Upper B", "2026-10-08", [(f"{base} · Dumbbell", 11), (f"{base} · Machine", 5)]),
        ])
        workouts = ap_text_import.parse_export(export)
        rows = [dict(r) for r in db.query("SELECT id,name,equipment,unit,load_mode FROM exercises")]
        from domains.gym.aliases import load_aliases
        aliases = load_aliases()
        import_ids = {
            item.equipment: int(ap_text_import.resolve_exercise_row(item, aliases, rows)["id"])
            for item in workouts[0].exercises
        }
        assert import_ids == {"Dumbbell": dumbbell_id, "Machine": barbell_id}

        seen_rows = []
        real_resolve = plans.resolve_exercise_row

        def spy(item, alias_map, candidate_rows):
            seen_rows.append({int(r["id"]): r for r in candidate_rows})
            return real_resolve(item, alias_map, candidate_rows)

        monkeypatch.setattr(plans, "resolve_exercise_row", spy)

        preview = plans.preview_seed(name, ap_workouts=workouts)

        for candidates in seen_rows:
            assert candidates[dumbbell_id]["equipment"] == "dumbbell"
            assert candidates[barbell_id]["equipment"] == "machine"
            assert candidates[barbell_id]["load_mode"] == "external"
        assert seen_rows
        by_id = {item["exercise_id"]: item for item in preview["days"][3]["exercises"]}
        assert (by_id[dumbbell_id]["rep_min"], by_id[dumbbell_id]["rep_max"]) == (11, 11)
        assert (by_id[barbell_id]["rep_min"], by_id[barbell_id]["rep_max"]) == (5, 5)
    finally:
        db.execute("DELETE FROM workout_sets WHERE exercise_id=ANY(%s)", (created,))
        db.execute("DELETE FROM exercises WHERE id=ANY(%s)", (created,))


def test_ap_rep_targets_match_plan_name_by_title_suffix_only():
    rows = {1: {"id": 1, "name": "Synthetic Curl", "equipment": None, "unit": "reps", "load_mode": "external"}}
    export = (
        '"Upper A · Day 2 · Week 7 · Plan 2";"2026-10-08 19:37 h";"1 hr"\n'
        '"1. Synthetic Curl · 12 reps"\n#;KG;REPS\n1;50;8\n\n'
        '"Upper A · Day 2 · Week 6 · Plan";"2026-10-01 19:37 h";"1 hr"\n'
        '"1. Synthetic Curl · 6 reps"\n#;KG;REPS\n1;50;8\n'
    )
    workouts = ap_text_import.parse_export(export)
    assert len(workouts) == 2

    targets = plans._ap_rep_targets(None, workouts, "Plan", rows, {1: 1})

    assert targets == {("Upper A", 1): 6}
