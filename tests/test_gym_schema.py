"""Gym-Migrationen sind wiederholbar und greifen nicht in Altspalten ein."""

from core.db import MIGRATIONS


def test_gym_migrations_add_idempotent_columns_and_alias_table():
    sql = "\n".join(MIGRATIONS).lower()

    assert "alter table workouts add column if not exists last_activity_at" in sql
    assert "alter table workouts add column if not exists ended_at" in sql
    assert "create table if not exists exercise_aliases" in sql
    assert "alter table exercises add column if not exists exercise_type" in sql
    assert "alter table exercises add column if not exists instructions" in sql
    assert "create unique index if not exists gym_single_open_idx" in sql


def test_gym_backfill_changes_only_new_session_columns():
    statements = [statement.lower() for statement in MIGRATIONS]
    backfills = [statement for statement in statements if "update workouts" in statement]

    assert any("set last_activity_at=created_at" in statement for statement in backfills)
    assert any("set ended_at=created_at" in statement for statement in backfills)
    assert all("set date=" not in statement and "set title=" not in statement for statement in backfills)


def test_gym_v2_plan_tables_are_idempotent_and_stay_separate_from_legacy_columns():
    statements = [statement.lower() for statement in MIGRATIONS]
    sql = "\n".join(statements)

    for table in ("gym_plans", "gym_plan_days", "gym_plan_exercises", "gym_workout_exercises"):
        assert f"create table if not exists {table}" in sql
    assert "create unique index if not exists gym_one_active_plan_idx" in sql
    gym_migrations = [statement for statement in statements if "gym_plan" in statement or "gym_workout_exercises" in statement]
    assert all("drop table" not in statement and "drop column" not in statement for statement in gym_migrations)
    # Bestehende Gym-Spalten werden ausschließlich idempotent erweitert.
    altering = [statement for statement in gym_migrations if "alter table" in statement]
    assert altering
    assert all("add column if not exists" in statement and "drop" not in statement for statement in altering)
    assert any("add column if not exists target_unit" in statement for statement in altering)
    assert any("add column if not exists removed" in statement for statement in altering)
