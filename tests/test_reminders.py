import re
from contextlib import nullcontext
from datetime import datetime, timedelta

from tools.reminders import ReminderStore


class ReminderMemoryDB:
    """Zustandsführende DB-Fälschung für Reminder-Migrationen und Zustellung."""

    def __init__(self, rows=None):
        self.now = datetime(2026, 10, 13, 12)
        self.table_exists = True
        self.columns = {
            "id", "text", "remind_at", "sent", "delivery_attempts",
            "retry_after", "created_at",
        }
        self.rows = rows or [{
            "id": 17,
            "text": "Termin",
            "remind_at": datetime(2026, 10, 12),
            "sent": False,
            "delivery_attempts": 0,
            "retry_after": None,
            "created_at": datetime(2026, 10, 12),
        }]

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
            due.append({key: row[key] for key in ("id", "text", "remind_at", "sent")})
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
            max_attempts, _retry_limit, reminder_id = params
            row = self._row(reminder_id)
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
            if not row["sent"]:
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
            default = 0 if " default 0" in normalized else None
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
    original = dict(database.rows[0])
    _install_fake_db(monkeypatch, database)
    store = ReminderStore()

    store.setup()
    store.setup()

    assert {"delivery_attempts", "retry_after", "claimed_at"} <= database.columns
    assert database.rows[0] == {**original, "claimed_at": None}


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
        assert [reminder.id for reminder in store.get_due()] == [17]
        store.mark_delivery_failed(17)
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
