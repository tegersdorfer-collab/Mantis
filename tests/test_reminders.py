from contextlib import nullcontext
from datetime import datetime

from tools.reminders import ReminderStore


def test_reminder_schema_migrates_delivery_retry_state(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, sql, params=None):
            statements.append(sql)

    monkeypatch.setattr("tools.reminders._db.cursor", lambda: nullcontext(Cursor()))

    ReminderStore().setup()

    schema = " ".join(statements).lower()
    assert "delivery_attempts" in schema
    assert "retry_after" in schema
    assert "add column if not exists" in schema


def test_get_due_waits_until_retry_backoff_expires_and_claims_due_rows(monkeypatch):
    statements = []
    row = {
        "id": 17,
        "text": "Termin",
        "remind_at": datetime(2026, 10, 12),
        "sent": False,
    }
    monkeypatch.setattr(
        "tools.reminders._db.query",
        lambda sql, params=(): (statements.append((sql, params)) or [row]),
    )
    monkeypatch.setattr(
        "tools.reminders._db.execute",
        lambda sql, params=(): statements.append((sql, params)),
    )

    reminders = ReminderStore().get_due()

    assert [reminder.id for reminder in reminders] == [17]
    select_sql, select_params = statements[0]
    assert "retry_after is null or retry_after <= now()" in select_sql.lower()
    assert select_params == ()
    assert "update reminders set sent = true" in statements[1][0].lower()
    assert statements[1][1] == ([17],)


def test_failed_delivery_sets_backoff_and_a_finite_attempt_limit(monkeypatch):
    statements = []
    monkeypatch.setattr(
        "tools.reminders._db.execute",
        lambda sql, params=(): statements.append((sql, params)),
    )

    ReminderStore().mark_delivery_failed(23)

    sql, params = statements[0]
    sql = sql.lower()
    assert "delivery_attempts = delivery_attempts + 1" in sql
    assert "retry_after" in sql
    assert "interval '1 minute'" in sql
    assert "interval '5 minutes'" in sql
    assert "delivery_attempts + 1 >= %s" in sql
    assert params == (3, 3, 23)


def test_next_due_seconds_includes_a_retry_backoff_deadline(monkeypatch):
    statements = []
    monkeypatch.setattr(
        "tools.reminders._db.query_one",
        lambda sql, params=(): (statements.append((sql, params)) or {"secs": 60}),
    )

    assert ReminderStore().next_due_seconds() == 60.0
    sql, params = statements[0]
    assert "retry_after" in sql.lower()
    assert params == ()
