"""
Zentrale Datenbank-Schicht für Mantis.
- Connection-Pool (thread-safe)
- pgvector-Registrierung pro Connection
- Sync- und Async-Helfer (Async läuft im Thread-Executor)
- Idempotenter Migrations-Runner
"""
import asyncio
import json
import logging
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
from pgvector.psycopg2 import register_vector

import config

log = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# Verbindungs-Fehler, bei denen die Connection tot ist (PG-Neustart, Netz weg).
# Solche Connections dürfen NICHT zurück in den Pool (würden ihn vergiften),
# und der Aufruf wird einmal mit frischer Connection wiederholt.
_CONN_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def init_pool(minconn: int = 1, maxconn: int = 20) -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn, maxconn, dsn=config.DATABASE_URL
        )
    log.info(f"DB-Pool initialisiert ({minconn}-{maxconn} Connections)")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def cursor(dict_rows: bool = True, vector: bool = False):
    """Borgt eine Connection aus dem Pool, gibt einen Cursor, committet automatisch.

    Tote Connections (PG-Neustart, Netz weg) werden verworfen statt zurückgelegt —
    sonst vergiftet eine einzige tote Connection den Pool dauerhaft.
    """
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    broken = False
    try:
        if vector:
            register_vector(conn)
        factory = psycopg2.extras.RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
    except _CONN_ERRORS:
        broken = True
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn, close=broken)


def _with_retry(fn):
    """Einen DB-Aufruf bei toter Connection genau einmal mit frischer wiederholen."""
    try:
        return fn()
    except _CONN_ERRORS as e:
        log.warning(f"DB-Verbindung tot ({e}), versuche einmal neu ...")
        return fn()


# ── Sync-Helfer ──────────────────────────────────────────────────────────────
# `params or None`: psycopg2 macht Platzhalter-Substitution nur wenn vars nicht
# None ist. Ein leeres Tuple würde literale %-Zeichen im SQL (z.B. ILIKE 'x%')
# als kaputten Platzhalter interpretieren → IndexError.

def query(sql: str, params: tuple = ()) -> list[dict]:
    def run():
        with cursor() as cur:
            cur.execute(sql, params or None)
            return [dict(r) for r in cur.fetchall()]
    return _with_retry(run)


def query_one(sql: str, params: tuple = ()) -> dict | None:
    def run():
        with cursor() as cur:
            cur.execute(sql, params or None)
            row = cur.fetchone()
            return dict(row) if row else None
    return _with_retry(run)


def execute(sql: str, params: tuple = ()) -> int:
    def run():
        with cursor() as cur:
            cur.execute(sql, params or None)
            return cur.rowcount
    return _with_retry(run)


def insert_returning(sql: str, params: tuple = ()):
    def run():
        with cursor() as cur:
            cur.execute(sql, params or None)
            row = cur.fetchone()
            if row is None:
                return None
            # RealDictCursor → erstes Value
            return list(row.values())[0]
    return _with_retry(run)


# ── Async-Helfer (DB-Calls in Thread, damit Event-Loop frei bleibt) ──────────

async def aquery(sql: str, params: tuple = ()) -> list[dict]:
    return await asyncio.to_thread(query, sql, params)


async def aquery_one(sql: str, params: tuple = ()) -> dict | None:
    return await asyncio.to_thread(query_one, sql, params)


async def aexecute(sql: str, params: tuple = ()) -> int:
    return await asyncio.to_thread(execute, sql, params)


async def ainsert_returning(sql: str, params: tuple = ()):
    return await asyncio.to_thread(insert_returning, sql, params)


# ── Migrationen ──────────────────────────────────────────────────────────────

MIGRATIONS = [
    # pgvector + Memories existieren bereits via LZG.setup(); hier alles Neue.

    # Habits
    """
    CREATE TABLE IF NOT EXISTS habits (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        emoji       TEXT DEFAULT '✅',
        cadence     TEXT DEFAULT 'daily',      -- daily | weekly | custom
        target_per_week INT DEFAULT 7,
        color       TEXT DEFAULT '#0ea5e9',
        active      BOOLEAN DEFAULT TRUE,
        sort_order  INT DEFAULT 0,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_logs (
        id        SERIAL PRIMARY KEY,
        habit_id  INT REFERENCES habits(id) ON DELETE CASCADE,
        date      DATE NOT NULL,
        done      BOOLEAN DEFAULT TRUE,
        note      TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (habit_id, date)
    );
    """,

    # Fitness
    """
    CREATE TABLE IF NOT EXISTS exercises (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        category    TEXT DEFAULT 'strength',   -- strength | cardio | mobility
        muscle      TEXT,
        unit        TEXT DEFAULT 'reps',       -- reps | km | min
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workouts (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        title       TEXT,
        type        TEXT DEFAULT 'strength',   -- strength | run | mobility | other
        duration_min INT,
        distance_km DOUBLE PRECISION,
        notes       TEXT,
        rpe         INT,                        -- rate of perceived exertion 1-10
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workout_sets (
        id          SERIAL PRIMARY KEY,
        workout_id  INT REFERENCES workouts(id) ON DELETE CASCADE,
        exercise_id INT REFERENCES exercises(id),
        set_index   INT DEFAULT 1,
        reps        INT,
        weight_kg   DOUBLE PRECISION,
        distance_km DOUBLE PRECISION,
        duration_s  INT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS training_plans (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        goal        TEXT,
        weeks       INT DEFAULT 8,
        plan_json   JSONB DEFAULT '{}',
        active      BOOLEAN DEFAULT TRUE,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Journal
    """
    CREATE TABLE IF NOT EXISTS journal_entries (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        mood        INT,                        -- 1-5
        energy      INT,                        -- 1-5
        content     TEXT,
        tags        TEXT[],
        author      TEXT DEFAULT 'timo',        -- timo | mantis
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Ernährung
    """
    CREATE TABLE IF NOT EXISTS meals (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        meal_type   TEXT DEFAULT 'snack',       -- breakfast | lunch | dinner | snack
        description TEXT NOT NULL,
        calories    INT,
        protein_g   DOUBLE PRECISION,
        carbs_g     DOUBLE PRECISION,
        fat_g       DOUBLE PRECISION,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Ziele
    """
    CREATE TABLE IF NOT EXISTS goals (
        id          SERIAL PRIMARY KEY,
        title       TEXT NOT NULL,
        category    TEXT DEFAULT 'general',     -- fitness | career | finance | personal
        target_value DOUBLE PRECISION,
        current_value DOUBLE PRECISION DEFAULT 0,
        unit        TEXT,
        deadline    DATE,
        status      TEXT DEFAULT 'active',      -- active | done | paused | dropped
        progress_pct INT DEFAULT 0,
        notes       TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Reiches Task-System (mantis-nativ): Arten, Unteraufgaben, Fortschritt, Archiv
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id           SERIAL PRIMARY KEY,
        title        TEXT NOT NULL,
        notes        TEXT,
        kind         TEXT DEFAULT 'task',       -- task | project | checklist
        status       TEXT DEFAULT 'todo',       -- todo | in_progress | done | archived
        priority     TEXT DEFAULT 'medium',     -- high | medium | low
        progress_pct INT DEFAULT 0,
        parent_id    INT REFERENCES tasks(id) ON DELETE CASCADE,
        due          TIMESTAMPTZ,
        sort_order   INT DEFAULT 0,
        created_at   TIMESTAMPTZ DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);",
    "CREATE INDEX IF NOT EXISTS tasks_parent_idx ON tasks(parent_id);",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_to TEXT DEFAULT 'user';",
    # Alfred→Mantis-Umbenennung: die Spalte hieß bis hier "alfred_result" — ein
    # blindes ADD COLUMN IF NOT EXISTS mantis_result hätte die alte Spalte samt
    # historischem Inhalt als tote, verwaiste Spalte zurückgelassen und daneben
    # eine neue, leere angelegt. RENAME COLUMN erhält die Werte; der Fallback
    # ADD COLUMN IF NOT EXISTS greift nur bei einer wirklich frischen Installation
    # (die nie eine alfred_result-Spalte hatte).
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='tasks' AND column_name='alfred_result')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_name='tasks' AND column_name='mantis_result')
        THEN
            ALTER TABLE tasks RENAME COLUMN alfred_result TO mantis_result;
        END IF;
    END $$;
    """,
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS mantis_result TEXT;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS rejection_reason TEXT;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS suggestion_status TEXT;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS execution_phase TEXT DEFAULT 'pending';",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS clarification_question TEXT;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS clarification_answer TEXT;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS hold_until TIMESTAMPTZ;",

    # Mantis' eigene Agenda (autonome To-dos)
    """
    CREATE TABLE IF NOT EXISTS agenda (
        id          SERIAL PRIMARY KEY,
        kind        TEXT NOT NULL,              -- briefing | checkin | research | nudge | review
        title       TEXT NOT NULL,
        payload     JSONB DEFAULT '{}',
        priority    INT DEFAULT 5,              -- 1 hoch .. 9 niedrig
        run_after   TIMESTAMPTZ DEFAULT NOW(),
        status      TEXT DEFAULT 'pending',     -- pending | done | skipped
        result      TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        done_at     TIMESTAMPTZ
    );
    """,

    # Selbst-Reflexionen
    """
    CREATE TABLE IF NOT EXISTS reflections (
        id          SERIAL PRIMARY KEY,
        kind        TEXT DEFAULT 'daily',       -- daily | interaction | weekly
        content     TEXT NOT NULL,
        insights    JSONB DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Chat-Nachrichten (für Dashboard-Chat, kanalübergreifend)
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id          SERIAL PRIMARY KEY,
        role        TEXT NOT NULL,              -- user | assistant
        content     TEXT NOT NULL,
        channel     TEXT DEFAULT 'telegram',    -- telegram | dashboard | autopilot
        meta        JSONB DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Event-Log (alles was Mantis tut → fürs Dashboard "Mind"/Activity)
    """
    CREATE TABLE IF NOT EXISTS events_log (
        id          SERIAL PRIMARY KEY,
        type        TEXT NOT NULL,              -- thought | action | tool | reflection | briefing | error
        summary     TEXT NOT NULL,
        detail      JSONB DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Tägliche Metrik-Snapshots (fürs Dashboard-Trending, schnell)
    """
    CREATE TABLE IF NOT EXISTS metrics_snapshots (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        data        JSONB DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (date)
    );
    """,

    # Health (mantis-nativ, gespeist aus iCloud Health.json – unabhängig von ai-dashboard)
    """
    CREATE TABLE IF NOT EXISTS health_data (
        date             DATE PRIMARY KEY,
        steps            INT,
        active_calories  INT,
        exercise_minutes INT,
        stand_hours      INT,
        distance         DOUBLE PRECISION,
        weight           DOUBLE PRECISION,
        body_fat         DOUBLE PRECISION,
        bmi              DOUBLE PRECISION,
        resting_hr       INT,
        hr_avg           INT,
        hr_max           INT,
        hr_min           INT,
        hrv              DOUBLE PRECISION,
        vo2max           DOUBLE PRECISION,
        sleep_duration   DOUBLE PRECISION,
        sleep_deep       DOUBLE PRECISION,
        sleep_rem        DOUBLE PRECISION,
        sleep_core       DOUBLE PRECISION,
        sleep_awake      DOUBLE PRECISION,
        blood_oxygen     DOUBLE PRECISION,
        body_temp        DOUBLE PRECISION,
        calories         INT,
        protein          DOUBLE PRECISION,
        carbs            DOUBLE PRECISION,
        fat              DOUBLE PRECISION,
        water            DOUBLE PRECISION,
        updated_at       TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Kalender (mantis-erstellte Events; gelesene Events kommen live aus ICS)
    """
    CREATE TABLE IF NOT EXISTS calendar_events (
        id          SERIAL PRIMARY KEY,
        uid         TEXT UNIQUE,
        title       TEXT NOT NULL,
        start_ts    TIMESTAMPTZ NOT NULL,
        end_ts      TIMESTAMPTZ,
        all_day     BOOLEAN DEFAULT FALSE,
        location    TEXT,
        notes       TEXT,
        source      TEXT DEFAULT 'mantis',
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS health_date_idx ON health_data(date DESC);",
    "CREATE INDEX IF NOT EXISTS calevents_start_idx ON calendar_events(start_ts);",

    # Settings (Key-Value, fürs Dashboard-Konfigurieren)
    """
    CREATE TABLE IF NOT EXISTS settings (
        key         TEXT PRIMARY KEY,
        value       JSONB NOT NULL,
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Indizes
    "CREATE INDEX IF NOT EXISTS habit_logs_date_idx ON habit_logs(date);",
    "CREATE INDEX IF NOT EXISTS workouts_date_idx ON workouts(date);",
    "CREATE INDEX IF NOT EXISTS meals_date_idx ON meals(date);",
    "CREATE INDEX IF NOT EXISTS journal_date_idx ON journal_entries(date);",
    "ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS prompts_answers JSONB;",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance FLOAT DEFAULT 0.5;",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0;",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_recalled TIMESTAMPTZ;",
    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS kg_linked BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE brain_notes ADD COLUMN IF NOT EXISTS zettel_id TEXT;",
    "CREATE UNIQUE INDEX IF NOT EXISTS brain_notes_zettel_idx ON brain_notes(zettel_id) WHERE zettel_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS agenda_status_idx ON agenda(status, run_after);",
    "CREATE INDEX IF NOT EXISTS events_log_created_idx ON events_log(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS chat_messages_created_idx ON chat_messages(created_at DESC);",

    # Schema-Drift-Fixes: diese Spalten/Tabellen existierten nur noch in der laufenden
    # DB (aus einer älteren, inzwischen entfernten Migration), nicht mehr im Code.
    # Ohne diese Statements würde eine Neuinstallation/ein Restore kaputt starten.
    "ALTER TABLE habits ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'day';",
    "ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS gcal_id TEXT;",
    """
    CREATE TABLE IF NOT EXISTS kg_entities (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        type        TEXT NOT NULL,
        description TEXT,
        aliases     TEXT[] DEFAULT '{}',
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (name, type)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_relations (
        id          SERIAL PRIMARY KEY,
        subject_id  INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
        predicate   TEXT NOT NULL,
        object_id   INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
        context     TEXT,
        confidence  FLOAT DEFAULT 0.8,
        source      TEXT DEFAULT 'extract',
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (subject_id, predicate, object_id)
    );
    """,

    # Web-Push-Subscriptions (PWA-Benachrichtigungen)
    """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id          SERIAL PRIMARY KEY,
        endpoint    TEXT NOT NULL UNIQUE,
        p256dh      TEXT NOT NULL,
        auth        TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Second Brain – Notizen & Verlinkungen
    """
    CREATE TABLE IF NOT EXISTS brain_notes (
        id          SERIAL PRIMARY KEY,
        title       TEXT NOT NULL,
        content     TEXT NOT NULL DEFAULT '',
        category    TEXT NOT NULL DEFAULT 'inbox',
        tags        TEXT[] DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'active',
        pinned      BOOLEAN DEFAULT FALSE,
        embedding   vector(1024),
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS brain_links (
        from_id  INTEGER NOT NULL REFERENCES brain_notes(id) ON DELETE CASCADE,
        to_id    INTEGER NOT NULL REFERENCES brain_notes(id) ON DELETE CASCADE,
        PRIMARY KEY (from_id, to_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS brain_notes_category_idx ON brain_notes (category);",
    "CREATE INDEX IF NOT EXISTS brain_notes_embedding_idx ON brain_notes USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS today_focus BOOLEAN DEFAULT FALSE;",
    """CREATE TABLE IF NOT EXISTS body_measurements (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        weight_kg   FLOAT,
        waist_cm    FLOAT,
        chest_cm    FLOAT,
        hips_cm     FLOAT,
        bicep_cm    FLOAT,
        thigh_cm    FLOAT,
        neck_cm     FLOAT,
        body_fat    FLOAT,
        notes       TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );""",
    "CREATE UNIQUE INDEX IF NOT EXISTS body_measurements_date_idx ON body_measurements (date);",
    """CREATE TABLE IF NOT EXISTS training_cycle_events (
        id          SERIAL PRIMARY KEY,
        date        DATE NOT NULL DEFAULT CURRENT_DATE,
        slot        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );""",
    "CREATE INDEX IF NOT EXISTS training_cycle_events_date_idx ON training_cycle_events (date DESC, id DESC);",
    "ALTER TABLE workout_sets ADD COLUMN IF NOT EXISTS rpe INT;",
    "ALTER TABLE workout_sets ADD COLUMN IF NOT EXISTS is_warmup BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE workout_sets ADD COLUMN IF NOT EXISTS is_failure BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE meals ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'done';",

    # LLM-Usage-Tracking (Token-Verbrauch + API-Kosten pro Call)
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        id            SERIAL PRIMARY KEY,
        provider      TEXT NOT NULL,           -- claude | ollama
        model         TEXT NOT NULL,
        purpose       TEXT DEFAULT 'chat',     -- chat-agent | background
        input_tokens  INT DEFAULT 0,
        output_tokens INT DEFAULT 0,
        cost_usd      NUMERIC(10,6) DEFAULT 0,
        created_at    TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS llm_usage_created_idx ON llm_usage (created_at DESC);",

    # Accountability & Momentum (v0: Daily Anchor + Der Block).
    # Genau EINE Zeile pro Tag (date PK). Die Spec verlangt priority/block_done/
    # block_output/evening_done/evening_note; die *_at-Timestamps + block_confirmed
    # sind reines State-Tracking, damit (a) der Scheduler jede Nachricht idempotent
    # nur einmal pro Tag sendet (übersteht Neustarts) und (b) der Message-Handler
    # weiß, worauf eine eingehende Antwort gerade zu buchen ist. evening_* bleiben
    # in v0 ungenutzt (Feature D „Evening Close-Out" kommt später) — bewusst schon
    # angelegt, damit später keine zweite Migration nötig ist.
    """
    CREATE TABLE IF NOT EXISTS daily_log (
        date                  DATE PRIMARY KEY,
        priority              TEXT,
        anchor_sent_at        TIMESTAMPTZ,
        block_started_at      TIMESTAMPTZ,
        block_confirmed       BOOLEAN DEFAULT FALSE,
        block_end_prompted_at TIMESTAMPTZ,
        block_done            BOOLEAN,
        block_output          TEXT,
        evening_done          BOOLEAN,
        evening_note          TEXT,
        created_at            TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Bestandsdaten mit dem alten Sentinel-Wert 'alfred' auf 'mantis' nachziehen
    # (Code liest/schreibt nach der Umbenennung nur noch 'mantis') — idempotent,
    # nach dem ersten Lauf matcht WHERE ...='alfred' keine Zeilen mehr.
    "UPDATE tasks SET assigned_to='mantis' WHERE assigned_to='alfred';",
    "UPDATE calendar_events SET source='mantis' WHERE source='alfred';",

    # ── Forge: semi-autonomes Entwicklungssystem ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS forge_tasks (
        id            SERIAL PRIMARY KEY,
        title         TEXT NOT NULL,
        description   TEXT,
        source        TEXT NOT NULL DEFAULT 'timo',
        priority      INTEGER NOT NULL DEFAULT 50,
        state         TEXT NOT NULL DEFAULT 'queued',
        worktree_path TEXT,
        branch        TEXT,
        spec_path     TEXT,
        plan_path     TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0,
        refusals      INTEGER NOT NULL DEFAULT 0,
        pause_reason  TEXT,
        paused_until  TIMESTAMPTZ,
        parked_reason TEXT,
        created_at    TIMESTAMPTZ DEFAULT NOW(),
        updated_at    TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS forge_journal (
        id         SERIAL PRIMARY KEY,
        task_id    INTEGER REFERENCES forge_tasks(id) ON DELETE CASCADE,
        ts         TIMESTAMPTZ DEFAULT NOW(),
        kind       TEXT NOT NULL,
        message    TEXT,
        tokens_in  INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0
    );
    """,
    "CREATE INDEX IF NOT EXISTS forge_journal_ts_idx ON forge_journal(ts DESC);",
    "CREATE INDEX IF NOT EXISTS forge_tasks_state_idx ON forge_tasks(state, priority DESC);",

    # forge_journal: Cache-Token-Spalten (siehe forge/runner.py RunResult) —
    # gemessen an einer echten Aufnahme lagen cache_read/cache_creation um
    # Größenordnungen über tokens_in/tokens_out; ohne diese Spalten würde
    # journal.log() Werte entgegennehmen, die nirgendwo persistiert werden.
    "ALTER TABLE forge_journal ADD COLUMN IF NOT EXISTS cache_read INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE forge_journal ADD COLUMN IF NOT EXISTS cache_creation INTEGER NOT NULL DEFAULT 0;",

    # forge_budget: je Nacht und Anbieter eine Zeile. Die Zahlen sind eine
    # vorsorgliche Bremse, KEINE genaue Buchhaltung — die Anbieter melden ihren
    # Reststand nicht, und die opencode-CLI reicht keine Response-Header durch.
    # Das verlässliche Signal ist erschoepft_seit, gesetzt aus einer echten
    # Rate-Limit-Antwort.
    """
    CREATE TABLE IF NOT EXISTS forge_budget (
        nacht          DATE NOT NULL,
        provider       TEXT NOT NULL,
        laeufe         INTEGER NOT NULL DEFAULT 0,
        tokens_in      BIGINT  NOT NULL DEFAULT 0,
        tokens_out     BIGINT  NOT NULL DEFAULT 0,
        erschoepft_seit TIMESTAMPTZ,
        grund          TEXT,
        PRIMARY KEY (nacht, provider)
    );
    """,

    # Welches Modell die implement/fix-Stufe zuletzt benutzt hat. Die
    # Review-Stufe liest das, um NICHT dasselbe Modell zu wählen: ein Modell,
    # das seinen eigenen Code abnimmt, sieht nur aus wie ein Review.
    "ALTER TABLE forge_tasks ADD COLUMN IF NOT EXISTS implement_model TEXT;",
]


def run_migrations() -> None:
    """Führt alle Migrationen idempotent aus."""
    init_pool()
    with cursor(dict_rows=False) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        for stmt in MIGRATIONS:
            cur.execute(stmt)
    log.info(f"Migrationen ausgeführt ({len(MIGRATIONS)} Statements)")


# ── Settings-Helfer ──────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    row = query_one("SELECT value FROM settings WHERE key = %s", (key,))
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, json.dumps(value)),
    )


def log_event(type_: str, summary: str, detail: dict | None = None) -> None:
    """Schreibt einen Eintrag ins Event-Log (fürs Dashboard 'Mantis Mind')."""
    try:
        execute(
            "INSERT INTO events_log (type, summary, detail) VALUES (%s, %s, %s)",
            (type_, summary[:500], json.dumps(detail or {})),
        )
    except Exception as e:
        log.debug(f"log_event fehlgeschlagen: {e}")


def log_error(source: str, exc: Exception) -> None:
    """Protokolliert einen Fehler fürs Dashboard-Widget 'Was ist heute schiefgelaufen'.
    Bewusst nur an den wichtigsten Stellen genutzt (nicht jeder der ~200 try/except-Blöcke
    im Code), damit das Widget auf echte Feature-Ausfälle zeigt statt auf Rauschen."""
    log_event("error", f"{source}: {exc}", {"source": source, "error": str(exc)})
