"""
Reminder-System – Mantis erinnert Timo zu bestimmten Zeiten.
Gespeichert in der mantis DB, wird im Denkzyklus geprüft.
Nutzt den zentralen Connection-Pool (core.db) – keine eigene Connection mehr.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from core import db as _db

log = logging.getLogger(__name__)

_MAX_DELIVERY_ATTEMPTS = 3
# Mantis läuft genau einmal (launchd + pidfile in main.py); die Lease dient nur der Crash-Recovery.
_CLAIM_LEASE_MINUTES = 2


@dataclass
class Reminder:
    id: int
    text: str
    remind_at: datetime
    sent: bool
    claimed_at: datetime | None = None


class ReminderStore:

    def setup(self) -> None:
        with _db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id         SERIAL PRIMARY KEY,
                    text       TEXT NOT NULL,
                    remind_at  TIMESTAMPTZ NOT NULL,
                    sent       BOOLEAN DEFAULT FALSE,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    retry_after TIMESTAMPTZ,
                    claimed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS "
                "delivery_attempts INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ"
            )
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"
            )

    def add(self, text: str, remind_at: datetime) -> int:
        with _db.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (text, remind_at) VALUES (%s, %s) RETURNING id",
                (text, remind_at),
            )
            rid = cur.fetchone()["id"]
        log.info(f"⏰ Reminder gesetzt: '{text}' um {remind_at.strftime('%d.%m. %H:%M')}")
        return rid

    def get_due(self) -> list[Reminder]:
        """Beansprucht fällige Reminder kurzzeitig, ohne sie vorab als gesendet zu markieren."""
        rows = _db.query(
            "WITH due AS ("
            "SELECT id FROM reminders "
            "WHERE remind_at <= NOW() AND sent = FALSE "
            "AND (retry_after IS NULL OR retry_after <= NOW()) "
            f"AND (claimed_at IS NULL OR claimed_at <= "
            f"NOW() - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes') "
            "ORDER BY remind_at ASC FOR UPDATE SKIP LOCKED"
            ") UPDATE reminders AS reminder SET claimed_at = NOW() "
            "FROM due WHERE reminder.id = due.id "
            "RETURNING reminder.id, reminder.text, reminder.remind_at, reminder.sent, "
            "reminder.claimed_at"
        )
        rows.sort(key=lambda row: row["remind_at"])
        return [Reminder(id=r["id"], text=r["text"],
                         remind_at=r["remind_at"], sent=r["sent"],
                         claimed_at=r["claimed_at"]) for r in rows]

    def mark_sent(self, reminder_id: int) -> None:
        """Bestätigt einen vollständig zugestellten Reminder."""
        _db.execute(
            "UPDATE reminders SET sent = TRUE, claimed_at = NULL, retry_after = NULL "
            "WHERE id = %s",
            (reminder_id,),
        )

    def release_claim(self, reminder_id: int, claimed_at: datetime | None) -> None:
        """Gibt einen ungesendeten Reminder nach Abbruch oder Fehler wieder frei."""
        _db.execute(
            "UPDATE reminders SET claimed_at = NULL "
            "WHERE id = %s AND sent = FALSE AND claimed_at = %s",
            (reminder_id, claimed_at),
        )

    def mark_delivery_failed(self, reminder_id: int, claimed_at: datetime | None) -> None:
        """Gibt einen fehlgeschlagenen Versand mit Backoff wieder frei.

        Nach drei fehlgeschlagenen Versuchen wird der Reminder beendet, damit
        ein dauerhaft unerreichbarer Kanal keine endlosen Zustellversuche auslöst.
        """
        _db.execute(
            "UPDATE reminders SET "
            "delivery_attempts = delivery_attempts + 1, "
            "sent = delivery_attempts + 1 >= %s, "
            "claimed_at = NULL, "
            "retry_after = CASE "
            "WHEN delivery_attempts + 1 >= %s THEN NULL "
            "WHEN delivery_attempts = 0 THEN NOW() + INTERVAL '1 minute' "
            "ELSE NOW() + INTERVAL '5 minutes' END "
            "WHERE id = %s AND sent = FALSE AND claimed_at = %s",
            (_MAX_DELIVERY_ATTEMPTS, _MAX_DELIVERY_ATTEMPTS, reminder_id, claimed_at),
        )

    def next_due_seconds(self) -> float | None:
        """Sekunden bis zum nächsten fälligen Reminder (oder None)."""
        row = _db.query_one(
            "SELECT EXTRACT(EPOCH FROM (MIN(COALESCE(retry_after, remind_at)) - NOW())) AS secs "
            "FROM reminders WHERE sent = FALSE "
            "AND COALESCE(retry_after, remind_at) > NOW()"
        )
        return float(row["secs"]) if row and row.get("secs") is not None else None

    def close(self) -> None:
        pass  # Pool-Verwaltung übernimmt core.db
