import json

import pytest

from core import db
from memory.chat_messages import PROMPT_SAFE_SQL


def test_prompt_safe_sql_has_no_literal_percent():
    assert "%" not in PROMPT_SAFE_SQL


def test_prompt_safe_sql_filters_empty_turns_before_the_history_limit():
    assert "content ~ '[^[:space:]]'" in PROMPT_SAFE_SQL


@pytest.mark.db
def test_prompt_safe_sql_works_in_parametrized_query():
    try:
        db.query_one("SELECT 1")
    except Exception:
        pytest.skip("Test-DB nicht erreichbar")
    db.run_migrations()
    # kzg_sessions.meta legt memory/lzg.py zur Laufzeit an; in der Test-DB ggf. nachziehen.
    db.execute("ALTER TABLE kzg_sessions ADD COLUMN IF NOT EXISTS meta JSONB DEFAULT '{}'::jsonb")
    marker = "prompt-safe-sql-test"
    db.execute("DELETE FROM kzg_sessions WHERE content LIKE %s", (marker + "%",))
    try:
        db.execute("INSERT INTO kzg_sessions (role, content, meta) VALUES (%s, %s, %s)",
                   ("assistant", marker + " ok", json.dumps({})))
        db.execute("INSERT INTO kzg_sessions (role, content, meta) VALUES (%s, %s, %s)",
                   ("assistant", "Agent-Remote " + marker, json.dumps({})))
        rows = db.query(
            f"SELECT role, content FROM kzg_sessions WHERE {PROMPT_SAFE_SQL} "
            "AND content LIKE %s ORDER BY created_at DESC LIMIT %s",
            ("%" + marker + "%", 10),
        )
        contents = [r["content"] for r in rows]
        assert marker + " ok" in contents
        assert not any(c.startswith("Agent-Remote") for c in contents)
    finally:
        db.execute("DELETE FROM kzg_sessions WHERE content LIKE %s", ("%" + marker + "%",))
