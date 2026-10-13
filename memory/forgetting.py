"""
memory/forgetting.py

Simuliert die Ebbinghaussche Vergessenskurve für das Mantis-Langzeitgedächtnis.

Formel:
    retention = exp(-t / S)
    S = importance * (1 + 0.25 * recall_count)   # Stabilität wächst mit Wiederholungen

    t = Tage seit last_recalled

Regeln:
  - retention < DELETE_THRESHOLD → Memory löschen
  - kg_linked = TRUE             → niemals löschen (im Wissens-Graphen verankert)
  - Chat-Nachrichten ohne Entity-Bezug → nach CHAT_TTL_DAYS löschen

Läuft stündlich im Idle-Loop des Orchestrators.
"""
import logging
import math
from datetime import datetime, timezone

from core import db as _db
import config

log = logging.getLogger(__name__)

DELETE_THRESHOLD = 0.10   # Retention unter 10% → vergessen


def compute_retention(importance: float, recall_count: int, days: float) -> float:
    """Ebbinghaus-Retention exp(-t/S).

    S (Stabilität) = importance * (1 + 0.25 * recall_count), aber mindestens 1.0,
    damit normale Memories nicht schon nach einem Tag unter die Löschschwelle fallen.
    """
    stability = max(importance * (1 + 0.25 * recall_count), 1.0)
    return math.exp(-days / stability)


def should_forget(importance: float, recall_count: int, days: float) -> bool:
    """True, wenn die Retention unter die Löschschwelle gefallen ist."""
    return compute_retention(importance, recall_count, days) < DELETE_THRESHOLD


class ForgettingCurve:

    async def run(self) -> dict:
        """
        Führt einen Vergessens-Durchlauf durch.
        Gibt Stats-Dict zurück: {checked, forgotten, chat_pruned}
        """
        forgotten   = await self._decay_memories()
        chat_pruned = await self._prune_chat()
        await self._importance_triage()
        return {"forgotten": forgotten, "chat_pruned": chat_pruned}

    async def _importance_triage(self) -> None:
        """
        Importance Triage: oft-abgerufene Memories steigen auf (max 1.0),
        nie-abgerufene sinken ab (min 0.1). Läuft im Vergessens-Durchlauf.
        """
        import asyncio
        # Hochstufen: recall_count >= 3 → importance += 0.1 (max 1.0)
        await asyncio.to_thread(
            _db.execute,
            """
            UPDATE memories
            SET importance = LEAST(importance + 0.1, 1.0)
            WHERE recall_count >= 3 AND importance < 1.0
            """,
        )
        # Abstufen: nie abgerufen und älter als 14 Tage → importance -= 0.05 (min 0.1)
        await asyncio.to_thread(
            _db.execute,
            """
            UPDATE memories
            SET importance = GREATEST(importance - 0.05, 0.1)
            WHERE recall_count = 0
              AND created_at < NOW() - INTERVAL '14 days'
              AND importance > 0.1
            """,
        )

    async def _decay_memories(self) -> int:
        """Löscht Memories mit zu niedriger Retention (außer kg_linked)."""
        import asyncio
        rows = await asyncio.to_thread(
            _db.query,
            """
            SELECT id, importance, recall_count, last_recalled, content
            FROM memories
            WHERE kg_linked = FALSE
            """,
        )
        now = datetime.now(timezone.utc)
        to_delete = []

        for r in rows:
            last = r["last_recalled"]
            if last is None:
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = (now - last).total_seconds() / 86400
            importance   = float(r["importance"]   or 0.5)
            recall_count = int(r["recall_count"]   or 0)

            retention = compute_retention(importance, recall_count, days)
            if retention < DELETE_THRESHOLD:
                to_delete.append((r["id"], r["content"][:60], round(retention, 3)))

        for mid, snippet, ret in to_delete:
            await asyncio.to_thread(_db.execute, "DELETE FROM memories WHERE id = %s", (mid,))
            log.info(f"🕐 Vergessen (ret={ret}): '{snippet}'")

        return len(to_delete)

    async def _prune_chat(self) -> int:
        """Löscht alte Chat-Nachrichten (einfaches TTL)."""
        import asyncio
        ttl_days = config.CHAT_TTL_DAYS
        result = await asyncio.to_thread(
            _db.query_one,
            """
            WITH deleted AS (
                DELETE FROM chat_messages
                WHERE created_at < NOW() - make_interval(days => %s)
                RETURNING id
            )
            SELECT count(*) AS n FROM deleted
            """,
            (ttl_days,),
        )
        n = int((result or {}).get("n", 0))
        if n:
            log.info(f"🗑️ {n} alte Chat-Nachrichten gelöscht (>{ttl_days}d)")
        return n

    def bump_recall(self, memory_id: int) -> None:
        """Wird aufgerufen wenn eine Memory im Kontext genutzt wurde → Stabilität erhöhen."""
        _db.execute(
            """
            UPDATE memories
            SET recall_count  = recall_count + 1,
                last_recalled = NOW()
            WHERE id = %s
            """,
            (memory_id,),
        )
