"""
Langzeitgedächtnis (LZG) – persistente Vektordatenbank via pgvector.
Speichert komprimierte Fakten, Muster und Erkenntnisse über Timo.

Alle DB-Operationen laufen über den zentralen Connection-Pool (core.db),
keine eigene psycopg2-Connection mehr → kein Race-Condition-Risiko.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

import config
from core import db as _db

log = logging.getLogger(__name__)


@dataclass
class Memory:
    id: int
    content: str
    category: str          # "fact" | "pattern" | "goal" | "correction" | "context"
    confidence: float      # 0.0 – 1.0
    created_at: datetime
    last_verified: Optional[datetime]
    metadata: dict


class LZG:
    """
    Langzeitgedächtnis mit semantischer Suche (pgvector).
    Nutzt den gemeinsamen DB-Pool — thread-sicher, kein Verbindungs-Thrashing.
    """

    def setup(self) -> None:
        """Erstellt Tabellen falls nicht vorhanden."""
        with _db.cursor(vector=True) as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id           SERIAL PRIMARY KEY,
                    content      TEXT NOT NULL,
                    embedding    vector(1024),
                    category     TEXT DEFAULT 'fact',
                    confidence   FLOAT DEFAULT 0.8,
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    last_verified TIMESTAMPTZ,
                    metadata     JSONB DEFAULT '{}'
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS memories_embedding_idx
                ON memories USING hnsw (embedding vector_cosine_ops);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kzg_sessions (
                    id         SERIAL PRIMARY KEY,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    meta       JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE kzg_sessions ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}';")

    def save(
        self,
        content: str,
        embedding: list[float] | np.ndarray,
        category: str = "fact",
        confidence: float = 0.8,
        metadata: dict | None = None,
    ) -> int:
        """
        ADD-only: Speichert eine neue Erinnerung, niemals Update.
        Wenn ein sehr ähnlicher Eintrag existiert (distance < 0.05), wird
        die neue Variante trotzdem gespeichert — kein stale-update-Bug möglich.
        Gibt ID zurück.
        """
        emb = np.array(embedding)
        # Warnung bei fast-identischem Duplikat (kein Block, nur Log)
        similars = self.find_similar(emb, threshold=0.05, top_k=1)
        if similars:
            log.debug(f"ADD-only: ähnliche Memory existiert (dist={similars[0][1]:.3f}), speichere trotzdem")
        with _db.cursor(vector=True) as cur:
            cur.execute(
                """
                INSERT INTO memories (content, embedding, category, confidence, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (content, emb, category, confidence,
                 json.dumps(metadata or {})),
            )
            return cur.fetchone()["id"]

    def search(
        self,
        query_embedding: list[float] | np.ndarray,
        top_k: int | None = None,
        category: str | None = None,
        min_confidence: float = 0.3,
    ) -> list[Memory]:
        """Semantische Suche im Langzeitgedächtnis."""
        k = top_k or config.LZG_TOP_K

        where = "confidence >= %s"
        params: list = [min_confidence]

        if category:
            where += " AND category = %s"
            params.append(category)

        params.append(np.array(query_embedding))
        params.append(k)

        with _db.cursor(vector=True) as cur:
            cur.execute(
                f"""
                SELECT id, content, category, confidence,
                       created_at, last_verified, metadata
                FROM memories
                WHERE {where}
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        return [_row_to_memory(r) for r in rows]

    def get_all(self, category: str | None = None, limit: int = 50) -> list[Memory]:
        """Alle Memories abrufen (ohne Vektor-Suche)."""
        if category:
            rows = _db.query(
                """
                SELECT id, content, category, confidence,
                       created_at, last_verified, metadata
                FROM memories
                WHERE category = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (category, limit),
            )
        else:
            rows = _db.query(
                """
                SELECT id, content, category, confidence,
                       created_at, last_verified, metadata
                FROM memories
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )

        return [_row_to_memory(r) for r in rows]

    def find_similar(
        self,
        embedding: list[float] | np.ndarray,
        threshold: float = 0.12,
        top_k: int = 3,
    ) -> list[tuple["Memory", float]]:
        """
        Findet semantisch sehr ähnliche Memories.
        Gibt Liste von (Memory, distance) zurück – kleinste distance = ähnlichster.
        """
        with _db.cursor(vector=True) as cur:
            cur.execute(
                """
                SELECT id, content, category, confidence,
                       created_at, last_verified, metadata,
                       embedding <=> %s AS distance
                FROM memories
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (np.array(embedding), np.array(embedding), top_k),
            )
            rows = cur.fetchall()

        result = []
        for r in rows:
            dist = float(r["distance"])
            if dist <= threshold:
                result.append((_row_to_memory(r), dist))
        return result

    @staticmethod
    def _is_temporal_recent(query: str) -> bool:
        """Erkennt ob die Anfrage nach aktuellem Zustand fragt (vs. historisch)."""
        recent_kw = {"heute", "jetzt", "aktuell", "gerade", "momentan", "diese woche",
                     "current", "today", "now", "currently", "this week"}
        hist_kw   = {"letzten", "letzte", "letzter", "damals", "früher", "januar",
                     "februar", "märz", "april", "mai", "juni", "juli", "august",
                     "september", "oktober", "november", "dezember", "last", "ago",
                     "before", "previously"}
        q = query.lower()
        is_recent  = any(w in q for w in recent_kw)
        is_history = any(w in q for w in hist_kw)
        if is_recent and not is_history:
            return True
        if is_history and not is_recent:
            return False
        return True  # default: bevorzuge Aktuelles

    def search_hybrid(
        self,
        query_text: str,
        query_embedding: list[float] | np.ndarray,
        top_k: int | None = None,
    ) -> list[Memory]:
        """
        Fusionierte Suche: pgvector-Cosine + Keyword-ILIKE.
        Deduplication per ID, semantic hits kommen zuerst.
        """
        k = top_k or config.LZG_TOP_K
        prefer_recent = self._is_temporal_recent(query_text)

        # pgvector — bei "aktuell"-Anfragen neuere Memories bevorzugen
        if prefer_recent:
            with _db.cursor(vector=True) as cur:
                cur.execute(
                    """
                    SELECT id, content, category, confidence,
                           created_at, last_verified, metadata,
                           embedding <=> %s AS vec_dist
                    FROM memories
                    WHERE confidence >= 0.3
                    ORDER BY (embedding <=> %s) * 0.7 + (EXTRACT(EPOCH FROM (NOW()-created_at))/86400/30) * 0.3
                    LIMIT %s
                    """,
                    (np.array(query_embedding), np.array(query_embedding), k),
                )
                semantic = [_row_to_memory(r) for r in cur.fetchall()]
        else:
            semantic = self.search(query_embedding, top_k=k)
        seen_ids = {m.id for m in semantic}

        # Keyword-Suche: einfaches ILIKE auf content für jedes Wort >3 Zeichen
        words = [w for w in query_text.lower().split() if len(w) > 3]
        keyword_hits: list[Memory] = []
        if words:
            # OR-Pattern auf content
            conditions = " OR ".join(["content ILIKE %s"] * len(words))
            params = [f"%{w}%" for w in words] + [k]
            with _db.cursor(vector=True) as cur:
                cur.execute(
                    f"""
                    SELECT id, content, category, confidence,
                           created_at, last_verified, metadata
                    FROM memories
                    WHERE ({conditions}) AND confidence >= 0.3
                    ORDER BY confidence DESC, created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                for r in cur.fetchall():
                    if r["id"] not in seen_ids:
                        keyword_hits.append(_row_to_memory(r))
                        seen_ids.add(r["id"])

        return semantic + keyword_hits

    def update_confidence(self, memory_id: int, confidence: float) -> None:
        _db.execute(
            "UPDATE memories SET confidence = %s, last_verified = NOW() WHERE id = %s",
            (confidence, memory_id),
        )

    def supersede(self, old_id: int, new_id: int, factor: float = 0.4) -> None:
        """Wertet einen überholten Fakt ab, statt ihn zu löschen (ADD-only-treu).

        Die Konfidenz wird multiplikativ gesenkt (Untergrenze 0.05, damit die
        Historie erhalten bleibt), und in metadata wird `superseded_by` vermerkt.
        Dadurch verliert der alte Fakt im Recall gegen den neuen, ohne dass Wissen
        hart verloren geht."""
        _db.execute(
            "UPDATE memories SET confidence = GREATEST(0.05, confidence * %s), "
            "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb WHERE id = %s",
            (factor, json.dumps({"superseded_by": new_id}), old_id),
        )

    def delete(self, memory_id: int) -> None:
        _db.execute("DELETE FROM memories WHERE id = %s", (memory_id,))

    # ── KZG-Session-Persistenz ──────────────────────────────────────────────

    def save_kzg_turn(self, role: str, content: str, meta: dict | None = None) -> None:
        _db.execute(
            "INSERT INTO kzg_sessions (role, content, meta) VALUES (%s, %s, %s)",
            (role, content, json.dumps(meta or {})),
        )

    def load_recent_kzg(self, max_turns: int = 10) -> list[dict]:
        from memory.chat_messages import PROMPT_SAFE_SQL, filter_prompt_messages

        rows = _db.query(
            f"""
            SELECT role, content, meta FROM kzg_sessions
            WHERE {PROMPT_SAFE_SQL}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (max_turns,),
        )
        return list(filter_prompt_messages(list(reversed(rows))))

    def clear_kzg_sessions(self, keep_last: int = 20) -> None:
        _db.execute(
            """
            DELETE FROM kzg_sessions
            WHERE id NOT IN (
                SELECT id FROM kzg_sessions
                ORDER BY created_at DESC
                LIMIT %s
            )
            """,
            (keep_last,),
        )

    def format_for_context(self, memories: list[Memory]) -> str:
        if not memories:
            return "Noch keine Langzeiterinnerungen vorhanden."
        lines = []
        for m in memories:
            conf_str = f"({int(m.confidence * 100)}% sicher)"
            lines.append(f"[{m.category.upper()}] {m.content} {conf_str}")
        return "\n".join(lines)

    # Kein close() mehr nötig – Pool-Verwaltung übernimmt core.db
    def close(self) -> None:
        pass


# ── Helfer ────────────────────────────────────────────────────────────────────

def _row_to_memory(r: dict) -> Memory:
    return Memory(
        id=r["id"],
        content=r["content"],
        category=r["category"],
        confidence=r["confidence"],
        created_at=r["created_at"],
        last_verified=r.get("last_verified"),
        metadata=r.get("metadata") or {},
    )
