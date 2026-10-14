import asyncio
import re
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.autopilot import Autopilot
from tools.reminders import ReminderStore


class ReminderMemoryDB:
    """Zustandsführende DB-Fälschung für Reminder-Migrationen und Zustellung."""

    def __init__(self, rows=None):
        self.now = datetime(2026, 10, 13, 12)
        self.table_exists = True
        self.columns = {"id", "text", "remind_at", "sent", "created_at"}
        self.column_defaults = {}
        self.rows = rows or [
            {
                "id": 17,
                "text": "Offener Altbestand",
                "remind_at": datetime(2026, 10, 12),
                "sent": False,
                "created_at": datetime(2026, 10, 12),
            },
            {
                "id": 18,
                "text": "Gesendeter Altbestand",
                "remind_at": datetime(2026, 10, 11),
                "sent": True,
                "created_at": datetime(2026, 10, 11),
            },
        ]

    def cursor(self):
        return nullcontext(_ReminderCursor(self))

    def query(self, sql, _params=()):
        claim_query = "returning" in sql.lower()
        has_claim_predicate = "claimed_at" in sql.lower()
        due = []
        for row in sorted(self.rows, key=lambda item: item["remind_at"]):
            if row["sent"] or row["remind_at"] > self.now:
                continue
            if row["retry_after"] is not None and row["retry_after"] > self.now:
                continue
            claimed_at = row.get("claimed_at")
            lease_expired = claimed_at is None or claimed_at <= self.now - timedelta(minutes=2)
            if has_claim_predicate and not lease_expired:
                continue
            if claim_query:
                row["claimed_at"] = self.now
            due.append({
                key: row[key]
                for key in ("id", "text", "remind_at", "sent", "claimed_at")
            })
        return due

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("update reminders set sent = true where id = any"):
            ids = params[0]
            for row in self.rows:
                if row["id"] in ids:
                    row["sent"] = True
            return len(ids)

        if normalized.startswith("update reminders set delivery_attempts ="):
            max_attempts, _retry_limit, reminder_id, claimed_at = params
            row = self._row(reminder_id)
            if row["sent"] or row["claimed_at"] != claimed_at:
                return 0
            previous_attempts = row["delivery_attempts"]
            attempts = previous_attempts + 1
            sent_sql = re.search(r"sent\s*=\s*([^,]+)", sql, re.I)
            if sent_sql is None:
                raise AssertionError("Retry-Update enthält keine sent-Zuweisung")
            row["delivery_attempts"] = attempts
            row["sent"] = self._evaluate_sent(sent_sql.group(1), attempts, max_attempts)
            if attempts >= max_attempts:
                row["retry_after"] = None
            else:
                minutes = 1 if previous_attempts == 0 else 5
                row["retry_after"] = self.now + timedelta(minutes=minutes)
            row["claimed_at"] = None
            return 1

        if normalized.startswith(
            "update reminders set sent = true, claimed_at = null, retry_after = null"
        ):
            row = self._row(params[0])
            row.update(sent=True, claimed_at=None, retry_after=None)
            return 1

        if normalized.startswith("update reminders set claimed_at = null"):
            row = self._row(params[0])
            if not row["sent"] and row["claimed_at"] == params[1]:
                row["claimed_at"] = None
            return 1

        raise AssertionError(f"Nicht unterstütztes Reminder-Statement: {sql}")

    @staticmethod
    def _evaluate_sent(expression, attempts, max_attempts):
        terms = re.split(r"\s+AND\s+", expression.strip(), flags=re.I)
        if not re.fullmatch(
            r"delivery_attempts\s*\+\s*1\s*>=\s*%s", terms[0].strip(), re.I
        ):
            raise AssertionError(f"Unerwartete sent-Bedingung: {expression}")
        result = attempts >= max_attempts
        for term in terms[1:]:
            if term.strip().upper() == "FALSE":
                result = result and False
            elif term.strip().upper() != "TRUE":
                raise AssertionError(f"Nicht unterstützte sent-Bedingung: {term}")
        return result

    def _row(self, reminder_id):
        return next(row for row in self.rows if row["id"] == reminder_id)


class _ReminderCursor:
    def __init__(self, database):
        self.database = database

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table"):
            if self.database.table_exists:
                if "create table if not exists reminders" in normalized:
                    return
                raise RuntimeError("relation reminders already exists")
            self.database.table_exists = True
            definitions = sql[sql.index("(") + 1:sql.rindex(")")]
            for definition in definitions.split(","):
                match = re.match(r"\s*(\w+)\s+", definition)
                if match and match.group(1).lower() != "primary":
                    self.database.columns.add(match.group(1).lower())
            return

        alter = re.fullmatch(
            r"alter table reminders add column (if not exists )?(\w+).*",
            normalized,
        )
        if alter:
            guarded, column = alter.groups()
            if column in self.database.columns:
                if guarded:
                    return
                raise RuntimeError(f"column {column} already exists")
            self.database.columns.add(column)
            default = 0 if re.search(r"\bdefault\s+0\b", normalized) else None
            self.database.column_defaults[column] = default
            for row in self.database.rows:
                row[column] = default
            return

        raise AssertionError(f"Nicht unterstützte Migration: {sql}")


def _install_fake_db(monkeypatch, database):
    monkeypatch.setattr("tools.reminders._db.cursor", database.cursor)
    monkeypatch.setattr("tools.reminders._db.query", database.query)
    monkeypatch.setattr("tools.reminders._db.execute", database.execute)


def test_reminder_migration_is_idempotent_and_preserves_existing_rows(monkeypatch):
    database = ReminderMemoryDB()
    original = [dict(row) for row in database.rows]
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()

    store.setup()
    store.setup()

    assert {"delivery_attempts", "retry_after", "claimed_at"} <= database.columns
    assert database.column_defaults["delivery_attempts"] == 0
    assert database.rows == [
        {**original[0], "kind": None, "delivery_attempts": 0, "retry_after": None, "claimed_at": None},
        {**original[1], "kind": None, "delivery_attempts": 0, "retry_after": None, "claimed_at": None},
    ]


def test_get_due_claims_without_marking_sent_and_expired_claim_can_retry(monkeypatch):
    database = ReminderMemoryDB()
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()
    store.setup()

    first = store.get_due()
    assert [reminder.id for reminder in first] == [17]
    assert first[0].sent is False
    assert database.rows[0]["sent"] is False
    assert database.rows[0]["claimed_at"] == database.now
    assert store.get_due() == []

    database.now += timedelta(minutes=2)
    retried = store.get_due()
    assert [reminder.id for reminder in retried] == [17]
    assert retried[0].sent is False


def test_stale_claim_failure_cannot_reset_a_confirmed_delivery(monkeypatch):
    database = ReminderMemoryDB(rows=[{
        "id": 17,
        "text": "Termin",
        "remind_at": datetime(2026, 10, 12),
        "sent": False,
        "created_at": datetime(2026, 10, 12),
    }])
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()
    store.setup()

    original_claim = store.get_due()[0]
    database.now += timedelta(minutes=2)
    replacement_claim = store.get_due()[0]
    assert replacement_claim.claimed_at != original_claim.claimed_at

    store.mark_sent(17)
    store.mark_delivery_failed(17, original_claim.claimed_at)

    assert database.rows[0]["sent"] is True
    assert database.rows[0]["delivery_attempts"] == 0
    assert database.rows[0]["retry_after"] is None


def test_confirmed_delivery_marks_sent_and_clears_its_claim(monkeypatch):
    database = ReminderMemoryDB()
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()
    store.setup()
    store.get_due()

    store.mark_sent(17)

    assert database.rows[0]["sent"] is True
    assert database.rows[0]["claimed_at"] is None
    assert database.rows[0]["retry_after"] is None
    assert store.get_due() == []


def test_retry_limit_marks_reminder_sent_and_removes_it_from_due_state(monkeypatch):
    database = ReminderMemoryDB()
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()
    store.setup()

    for attempt in range(3):
        claimed = store.get_due()
        assert [reminder.id for reminder in claimed] == [17]
        store.mark_delivery_failed(17, claimed[0].claimed_at)
        if attempt < 2:
            database.now = database.rows[0]["retry_after"]

    assert database.rows[0]["delivery_attempts"] == 3
    assert database.rows[0]["sent"] is True
    assert store.get_due() == []


def test_next_due_seconds_includes_a_retry_backoff_deadline(monkeypatch):
    monkeypatch.setattr(
        "tools.reminders._db.query_one",
        lambda _sql, params=(): {"secs": 60} if params == () else None,
    )

    assert ReminderStore().next_due_seconds() == 60.0


def test_next_due_returns_text_and_timestamp_for_situation_snapshot(monkeypatch):
    remind_at = datetime(2026, 10, 14, 14, 30)
    monkeypatch.setattr(
        "tools.reminders._db.query_one",
        lambda _sql, params=(): {"id": 25, "text": "Zahnarzt anrufen", "remind_at": remind_at},
    )

    reminder = ReminderStore().next_due()

    assert reminder == {"id": 25, "text": "Zahnarzt anrufen", "remind_at": remind_at}


def test_stop_next_timer_marks_only_an_unsent_timer_as_sent(monkeypatch):
    calls = []

    def execute(sql, params=()):
        calls.append((sql, params))
        return 1

    monkeypatch.setattr("tools.reminders._db.execute", execute)

    assert ReminderStore().stop_next_timer() is True
    assert calls[0][1] == ("timer",)
    assert "sent = FALSE" in calls[0][0]


def test_autopilot_retries_delivery_confirmation_without_resending_after_lease(monkeypatch):
    database = ReminderMemoryDB()
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()
    store.setup()

    mark_sent_calls = []
    original_execute = database.execute

    def execute(sql, params=()):
        if sql.lower().startswith("update reminders set sent = true, claimed_at"):
            mark_sent_calls.append(params[0])
            if len(mark_sent_calls) == 1:
                raise RuntimeError("temporary database failure")
        return original_execute(sql, params)

    monkeypatch.setattr("tools.reminders._db.execute", execute)
    sends = []

    async def send_plain(text):
        sends.append(text)

    autopilot = Autopilot(
        llm=None,
        lzg=None,
        dashboard=None,
        reminders=store,
        channel=SimpleNamespace(send=send_plain, send_plain=send_plain),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )

    async def after_send(_text, _kind):
        pass

    async def deep_execution():
        return False

    autopilot._after_send = after_send
    autopilot._tick_deep_execution = deep_execution
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)

    assert asyncio.run(autopilot.tick()) is True
    assert mark_sent_calls == [17]
    assert database.rows[0]["sent"] is False

    database.now += timedelta(minutes=2)

    assert asyncio.run(autopilot.tick()) is False
    assert sends == ["⏰ Erinnerung: Offener Altbestand"]
    assert mark_sent_calls == [17, 17]
    assert database.rows[0]["sent"] is True
    assert database.rows[0]["claimed_at"] is None


def test_stop_next_timer_ignores_reminders_with_timer_text_prefix(monkeypatch):
    calls = []

    def execute(sql, params=()):
        calls.append((sql, params))
        return 0

    monkeypatch.setattr("tools.reminders._db.execute", execute)

    assert ReminderStore().stop_next_timer() is False
    assert "LIKE" not in calls[0][0]
    assert "kind = %s" in calls[0][0]
