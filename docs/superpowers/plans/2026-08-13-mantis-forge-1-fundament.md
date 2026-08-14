# Mantis Forge — Plan 1: Fundament

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein eigenständiger Daemon läuft unter launchd, holt sich den obersten Task aus einer Postgres-Queue, legt dafür einen isolierten git-Worktree an, führt darin genau einen headless Claude-Code-Run aus und protokolliert alles — ohne irgendetwas zu mergen.

**Architecture:** Neues Paket `forge/` im Mantis-Repo, eigener launchd-Job, eigener Prozess. Die Forge importiert von Mantis ausschließlich `core.db` (Bibliothek, kein Prozess-Zugriff); die Kommunikation mit dem *laufenden* Mantis kommt erst in Plan 3 über HTTP dazu. Zustand liegt in zwei neuen Tabellen, Artefakte auf der Platte — dadurch ist jeder Absturz auf Stufengrenze wiederaufsetzbar.

**Tech Stack:** Python 3.14, psycopg2 über `core/db.py`, `subprocess` gegen die `claude`-CLI, `git worktree`, launchd. Keine neuen Dependencies.

**Spec:** `docs/superpowers/specs/2026-08-13-mantis-forge-design.md` (§2, §3, §4, §9, §10, §11)

## Global Constraints

- Python 3.14. **Keine neuen Dependencies** — stdlib plus was `core/db.py` schon mitbringt.
- Ruff-Konfiguration ist gesetzt: `select = ["F", "E9"]`, `line-length = 120`. Jeder Task endet mit sauberem `ruff check`.
- **Kommentare und Docstrings auf Deutsch** — Konvention der Codebase.
- Tests liegen in `tests/test_forge_*.py` und beginnen mit der Präambel der Codebase:
  ```python
  import sys
  import os
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
  ```
- **Keine Tests gegen die echte Postgres.** DB-Zugriffe werden per `monkeypatch.setattr(<modul>.db, "query", ...)` gestubbt — etabliertes Muster, siehe `tests/test_tasks_status.py:22-25`. Einzige Ausnahme: die Worktree-Tests laufen gegen ein frisch angelegtes temporäres echtes git-Repo (hermetisch, kein Netz).
- **Die Forge darf ausschließlich `core.db` aus Mantis importieren.** Kein `orchestrator`, kein `core.agent`, kein `web.*`. Diese Grenze ist der Grund, warum die Forge den Assistant nicht mit in den Abgrund reißt.
- SQL immer parametrisiert (`%s`). Literale `%` in SQL sind wegen psycopg2 verboten.
- Gearbeitet wird auf Branch `forge/fundament`, ein Commit pro Task.

## Abweichung von der Spec (bewusst)

**Pausen sind keine Zustände.** Spec §4 führt `paused_ratelimit` und `paused_user` als Zustände. Im Plan sind sie zwei orthogonale Spalten: `pause_reason` und `paused_until`. Grund: als Zustände bräuchte man zusätzlich ein `resume_state`-Feld, um zu wissen, wohin nach der Pause zurückgesprungen wird — doppelte Buchführung, die auseinanderlaufen kann. Als Spalten bleibt `state` immer die Pipeline-Stufe, und der Daemon überspringt schlicht jeden Task, dessen `paused_until` in der Zukunft liegt. Das Verhalten aus der Spec bleibt identisch.

**Keine `stage`-Spalte.** Spec §10 listet `state` *und* `stage`. Das wären zwei Namen für dieselbe Information — `state` **ist** die Pipeline-Stufe. Die Spalte entfällt, damit sie nicht auseinanderlaufen kann.

---

## File Structure

| Datei | Verantwortung |
|---|---|
| `forge/__init__.py` | Paket-Marker, Pfad-Konstanten (`MANTIS_REPO`, `FORGE_ROOT`) |
| `forge/models.py` | Zustandsnamen, erlaubte Übergänge, Task-Dataclass. Reine Logik, kein I/O |
| `forge/queue.py` | Task-CRUD gegen `forge_tasks`: einreihen, übernehmen, Zustand setzen, parken |
| `forge/journal.py` | Ereignis-Log gegen `forge_journal`: schreiben und lesen |
| `forge/gitctl.py` | Jeder git-Aufruf der Forge, mit Stale-Lock-Preflight davor |
| `forge/worktree.py` | Worktrees anlegen und entfernen, Branch-Namen |
| `forge/runner.py` | `claude -p` starten, `stream-json` parsen, Token-Verbrauch extrahieren |
| `forge/daemon.py` | Hauptschleife, Not-Aus, Pause bei interaktiver Session, Fehler-Spirale |
| `core/db.py` | *(ändern)* zwei Migrationen anhängen |
| `~/Library/LaunchAgents/com.mantis.forge.plist` | launchd-Job |

---

### Task 1: Zustandsmodell und DB-Schema

**Files:**
- Create: `forge/__init__.py`
- Create: `forge/models.py`
- Modify: `core/db.py` — zwei Migrationen ans Ende der `MIGRATIONS`-Liste (direkt vor der schließenden `]` bei ca. Zeile 599)
- Test: `tests/test_forge_models.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `forge.MANTIS_REPO: Path`, `forge.FORGE_ROOT: Path`
  - `forge.models.QUEUED/SPECCING/PLANNING/IMPLEMENTING/REVIEWING/GATING/AWAITING_RESTART/MERGED/PARKED/FAILED: str`
  - `forge.models.ACTIVE_STATES: frozenset[str]`
  - `forge.models.can_transition(current: str, target: str) -> bool`
  - Tabellen `forge_tasks`, `forge_journal`

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_models.py`:

```python
"""Unit-Tests für das Zustandsmodell der Forge — reine Logik, kein I/O."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import models as m


class TestTransitions:
    def test_queued_darf_nach_speccing(self):
        assert m.can_transition(m.QUEUED, m.SPECCING) is True

    def test_queued_darf_nicht_direkt_nach_merged(self):
        # Ein Task muss durch die Pipeline — Abkürzungen sind genau der Fehler,
        # den das Gate verhindern soll.
        assert m.can_transition(m.QUEUED, m.MERGED) is False

    def test_reviewing_darf_zurueck_nach_implementing(self):
        # Fix-Runde nach negativem Review-Verdict.
        assert m.can_transition(m.REVIEWING, m.IMPLEMENTING) is True

    def test_gating_darf_direkt_nach_merged(self):
        # Docs-only-Merges brauchen kein Neustart-Fenster (Spec §6.2 Schritt 1).
        assert m.can_transition(m.GATING, m.MERGED) is True

    def test_merged_ist_endzustand(self):
        assert m.can_transition(m.MERGED, m.QUEUED) is False

    def test_parked_darf_zurueck_in_die_queue(self):
        # Freigabe durch Timo im Dashboard.
        assert m.can_transition(m.PARKED, m.QUEUED) is True

    def test_jede_stufe_darf_parken(self):
        for state in [m.SPECCING, m.PLANNING, m.IMPLEMENTING, m.REVIEWING, m.GATING]:
            assert m.can_transition(state, m.PARKED) is True

    def test_unbekannter_zustand_ist_kein_uebergang(self):
        assert m.can_transition("voelliger_quatsch", m.QUEUED) is False


class TestActiveStates:
    def test_aktive_zustaende_sind_die_pipeline_stufen(self):
        assert m.ACTIVE_STATES == frozenset({
            m.SPECCING, m.PLANNING, m.IMPLEMENTING,
            m.REVIEWING, m.GATING, m.AWAITING_RESTART,
        })

    def test_queued_ist_nicht_aktiv(self):
        # Sonst würde der Daemon einen wartenden Task für laufend halten.
        assert m.QUEUED not in m.ACTIVE_STATES

    def test_endzustaende_sind_nicht_aktiv(self):
        for state in [m.MERGED, m.PARKED, m.FAILED]:
            assert state not in m.ACTIVE_STATES
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_models.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'forge'`

- [ ] **Step 3: Paket-Marker anlegen**

`forge/__init__.py`:

```python
"""Mantis Forge — semi-autonomes Entwicklungssystem.

Eigener Prozess, eigener launchd-Job. Importiert aus Mantis ausschließlich
`core.db`; mit dem laufenden Assistant wird nur über HTTP geredet. Diese Grenze
sorgt dafür, dass ein Absturz der Forge den Concierge nicht mitnimmt.
"""
from pathlib import Path

# Das Haupt-Checkout, auf dem Mantis produktiv läuft. Hier wird NIE gearbeitet.
MANTIS_REPO = Path(__file__).resolve().parent.parent

# Wurzel für die isolierten Worktrees — bewusst außerhalb des Repos, damit
# Suchen, Linter und die Test-Suite sie nicht mit einsammeln.
FORGE_ROOT = Path.home() / "Mantis-forge"
```

- [ ] **Step 4: Zustandsmodell implementieren**

`forge/models.py`:

```python
"""Zustandsmodell der Forge — welche Übergänge erlaubt sind und welche nicht.

Reine Logik ohne I/O, damit die Regeln testbar sind, ohne eine Datenbank zu
brauchen. `state` ist immer die Pipeline-Stufe; Pausen (Rate-Limit, Not-Aus)
sind bewusst KEINE Zustände, sondern eigene Spalten (pause_reason,
paused_until) — sonst bräuchte man zusätzlich ein resume_state-Feld.
"""

QUEUED = "queued"
SPECCING = "speccing"
PLANNING = "planning"
IMPLEMENTING = "implementing"
REVIEWING = "reviewing"
GATING = "gating"
AWAITING_RESTART = "awaiting_restart_window"
MERGED = "merged"
PARKED = "parked"
FAILED = "failed"

# Zustände, in denen ein Task als "in Arbeit" gilt. Findet der Daemon beim Start
# einen solchen Task, setzt er dort wieder auf, statt einen neuen zu ziehen.
ACTIVE_STATES = frozenset({
    SPECCING, PLANNING, IMPLEMENTING, REVIEWING, GATING, AWAITING_RESTART,
})

# Jede Pipeline-Stufe darf jederzeit parken oder endgültig scheitern.
_ESCAPES = {PARKED, FAILED}

_TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {SPECCING} | _ESCAPES,
    SPECCING: {PLANNING} | _ESCAPES,
    PLANNING: {IMPLEMENTING} | _ESCAPES,
    IMPLEMENTING: {REVIEWING} | _ESCAPES,
    # Negatives Review-Verdict schickt den Task in die Fix-Runde zurück.
    REVIEWING: {GATING, IMPLEMENTING} | _ESCAPES,
    # MERGED direkt aus GATING: Docs-only-Diffs brauchen kein Neustart-Fenster.
    GATING: {AWAITING_RESTART, MERGED} | _ESCAPES,
    AWAITING_RESTART: {MERGED, PARKED},
    MERGED: set(),                  # Endzustand
    PARKED: {QUEUED},               # Freigabe durch Timo
    FAILED: {QUEUED},               # manueller Neuanlauf
}


def can_transition(current: str, target: str) -> bool:
    """Ist der Übergang erlaubt? Unbekannte Zustände sind immer nein."""
    return target in _TRANSITIONS.get(current, set())
```

- [ ] **Step 5: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_models.py -v`
Expected: PASS, 13 Tests

- [ ] **Step 6: Migrationen anhängen**

In `core/db.py` die beiden folgenden Einträge direkt **vor** der schließenden `]` der `MIGRATIONS`-Liste einfügen — also nach der Zeile `"UPDATE calendar_events SET source='mantis' WHERE source='alfred';",`:

```python
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
```

- [ ] **Step 7: Migrationen gegen die echte DB fahren**

Run: `python -c "from core import db; db.run_migrations()"`
Expected: Logzeile `Migrationen ausgeführt (N Statements)`, kein Traceback

Run: `psql "$DATABASE_URL" -c "\d forge_tasks"`
Expected: Tabelle mit 17 Spalten, `id` als `integer NOT NULL DEFAULT nextval(...)`

- [ ] **Step 8: Lint und Commit**

```bash
ruff check forge/ core/db.py tests/test_forge_models.py
git add forge/__init__.py forge/models.py core/db.py tests/test_forge_models.py
git commit -m "feat(forge): Zustandsmodell und DB-Schema"
```

---

### Task 2: Queue

**Files:**
- Create: `forge/queue.py`
- Test: `tests/test_forge_queue.py`

**Interfaces:**
- Consumes: `forge.models` (Zustandsnamen, `ACTIVE_STATES`, `can_transition`)
- Produces:
  - `enqueue(title: str, description: str = "", source: str = "timo", priority: int = 50) -> int`
  - `active() -> dict | None`
  - `claim_next() -> dict | None`
  - `set_state(task_id: int, target: str, current: str) -> bool`
  - `park(task_id: int, current: str, reason: str) -> bool`
  - `pause(task_id: int, reason: str, until: datetime) -> None`

`claim_next()` liefert zuerst einen bereits laufenden Task zurück (Wiederaufsetzen nach Absturz) und erst dann den nächsten aus der Queue. Genau das macht den Daemon absturzfest, ohne dass er selbst Buch führen muss.

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_queue.py`:

```python
"""Unit-Tests für die Queue der Forge. DB gestubbt — das Muster stammt aus
tests/test_tasks_status.py: das db-Modul im Ziel-Modul wird ersetzt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

from forge import models as m
from forge import queue as q


class _Recorder:
    """Nimmt SQL und Parameter auf, statt sie an Postgres zu schicken."""

    def __init__(self, rows=None, returning=1):
        self.rows = rows if rows is not None else []
        self.returning = returning
        self.queries: list[tuple] = []
        self.executes: list[tuple] = []

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows

    def query_one(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows[0] if self.rows else None

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return 1

    def insert_returning(self, sql, params=()):
        self.executes.append((sql, params))
        return self.returning


def _patch(monkeypatch, rows=None, returning=1):
    rec = _Recorder(rows=rows, returning=returning)
    for name in ("query", "query_one", "execute", "insert_returning"):
        monkeypatch.setattr(q.db, name, getattr(rec, name))
    return rec


class TestEnqueue:
    def test_gibt_neue_id_zurueck(self, monkeypatch):
        _patch(monkeypatch, returning=42)
        assert q.enqueue("X5-Weckroutine") == 42

    def test_parameter_landen_in_der_richtigen_reihenfolge(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.enqueue("Titel", "Beschreibung", source="roadmap", priority=90)
        assert rec.executes[-1][1] == ("Titel", "Beschreibung", "roadmap", 90)

    def test_default_ist_timo_mit_prio_50(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.enqueue("Titel")
        assert rec.executes[-1][1] == ("Titel", "", "timo", 50)


class TestActive:
    def test_findet_laufenden_task(self, monkeypatch):
        _patch(monkeypatch, rows=[{"id": 7, "state": m.IMPLEMENTING}])
        assert q.active()["id"] == 7

    def test_fragt_nur_nach_aktiven_zustaenden(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active()
        gefragte_zustaende = set(rec.queries[-1][1][0])
        assert gefragte_zustaende == set(m.ACTIVE_STATES)

    def test_ohne_treffer_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.active() is None

    def test_pausierte_tasks_zaehlen_nicht_als_aktiv(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active()
        # Ein Task, der bis morgen pausiert ist, darf den Daemon nicht blockieren.
        assert "paused_until IS NULL OR paused_until <= NOW()" in rec.queries[-1][0]


class TestClaimNext:
    def test_laufender_task_hat_vorrang(self, monkeypatch):
        # Wiederaufsetzen nach Absturz: kein neuer Task, solange einer offen ist.
        rec = _patch(monkeypatch, rows=[{"id": 3, "state": m.PLANNING}])
        task = q.claim_next()
        assert task["id"] == 3
        assert rec.executes == []  # kein Zustandswechsel

    def test_leere_queue_gibt_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.claim_next() is None


class TestSetState:
    def test_erlaubter_uebergang_wird_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.PLANNING, current=m.SPECCING) is True
        assert rec.executes[-1][1] == (m.PLANNING, 1)

    def test_verbotener_uebergang_schreibt_nichts(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.set_state(1, m.MERGED, current=m.QUEUED) is False
        assert rec.executes == []  # lieber nichts als ein korrupter Zustand

    def test_updated_at_wird_mitgezogen(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.set_state(1, m.PLANNING, current=m.SPECCING)
        assert "updated_at=NOW()" in rec.executes[-1][0]


class TestPark:
    def test_park_schreibt_grund(self, monkeypatch):
        rec = _patch(monkeypatch)
        assert q.park(5, current=m.GATING, reason="Tests rot") is True
        assert rec.executes[-1][1] == (m.PARKED, "Tests rot", 5)


class TestPause:
    def test_pause_setzt_grund_und_zeitpunkt(self, monkeypatch):
        rec = _patch(monkeypatch)
        bis = datetime.now(timezone.utc) + timedelta(hours=1)
        q.pause(9, "ratelimit", bis)
        assert rec.executes[-1][1] == ("ratelimit", bis, 9)
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_queue.py -v`
Expected: FAIL mit `ImportError: cannot import name 'queue' from 'forge'`

- [ ] **Step 3: Queue implementieren**

`forge/queue.py`:

```python
"""Task-Queue der Forge — die einzige Stelle, die `forge_tasks` anfasst.

Zustandswechsel gehen ausnahmslos über `set_state`, das gegen das Modell in
forge/models.py prüft. Ein verbotener Übergang schreibt lieber gar nichts, als
einen Zustand zu hinterlassen, dem der Daemon danach nicht mehr trauen kann.
"""
import logging
from datetime import datetime

from core import db

from forge import models as m

log = logging.getLogger(__name__)


def enqueue(title: str, description: str = "", source: str = "timo", priority: int = 50) -> int:
    """Reiht einen neuen Task ein und gibt seine ID zurück."""
    return db.insert_returning(
        "INSERT INTO forge_tasks (title, description, source, priority) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (title, description, source, priority),
    )


def active() -> dict | None:
    """Der Task, der gerade in Arbeit ist — oder None.

    Pausierte Tasks (Rate-Limit, Not-Aus) gelten NICHT als aktiv, sonst würde
    eine Pause den Daemon für ihre gesamte Dauer blockieren.
    """
    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = ANY(%s) AND (paused_until IS NULL OR paused_until <= NOW()) "
        "ORDER BY updated_at ASC LIMIT 1",
        (list(m.ACTIVE_STATES),),
    )
    return rows[0] if rows else None


def claim_next() -> dict | None:
    """Der Task, an dem als Nächstes gearbeitet wird.

    Zuerst ein bereits laufender (Wiederaufsetzen nach Absturz), sonst der
    oberste aus der Queue — der wird dabei auf die erste Stufe gesetzt.
    """
    laufend = active()
    if laufend is not None:
        return laufend

    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = %s AND (paused_until IS NULL OR paused_until <= NOW()) "
        "ORDER BY priority DESC, id ASC LIMIT 1",
        (m.QUEUED,),
    )
    if not rows:
        return None

    task = rows[0]
    if not set_state(task["id"], m.SPECCING, current=m.QUEUED):
        return None
    task["state"] = m.SPECCING
    return task


def set_state(task_id: int, target: str, current: str) -> bool:
    """Setzt den Zustand, sofern der Übergang erlaubt ist. Sonst False."""
    if not m.can_transition(current, target):
        log.warning(f"Forge: verbotener Übergang {current} → {target} (Task {task_id})")
        return False
    db.execute(
        "UPDATE forge_tasks SET state=%s, updated_at=NOW() WHERE id=%s",
        (target, task_id),
    )
    return True


def park(task_id: int, current: str, reason: str) -> bool:
    """Legt den Task zur manuellen Sichtung beiseite. Der Worktree bleibt stehen."""
    if not m.can_transition(current, m.PARKED):
        return False
    db.execute(
        "UPDATE forge_tasks SET state=%s, parked_reason=%s, updated_at=NOW() WHERE id=%s",
        (m.PARKED, reason, task_id),
    )
    return True


def pause(task_id: int, reason: str, until: datetime) -> None:
    """Pausiert den Task bis `until`, ohne die Stufe zu verlassen."""
    db.execute(
        "UPDATE forge_tasks SET pause_reason=%s, paused_until=%s, updated_at=NOW() WHERE id=%s",
        (reason, until, task_id),
    )
```

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_queue.py -v`
Expected: PASS, 14 Tests

- [ ] **Step 5: Lint und Commit**

```bash
ruff check forge/queue.py tests/test_forge_queue.py
git add forge/queue.py tests/test_forge_queue.py
git commit -m "feat(forge): Task-Queue mit geprüften Zustandsübergängen"
```

---

### Task 3: Journal

**Files:**
- Create: `forge/journal.py`
- Test: `tests/test_forge_journal.py`

**Interfaces:**
- Consumes: `core.db`
- Produces:
  - `forge.journal.log(task_id: int | None, kind: str, message: str = "", tokens_in: int = 0, tokens_out: int = 0) -> None`
  - `forge.journal.recent(limit: int = 20) -> list[dict]`
  - `forge.journal.for_task(task_id: int) -> list[dict]`
  - `forge.journal.KINDS: frozenset[str]`

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_journal.py`:

```python
"""Unit-Tests für das Ereignis-Log der Forge. DB gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import journal as j


class _Recorder:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executes: list[tuple] = []
        self.queries: list[tuple] = []

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return 1

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        return self.rows


def _patch(monkeypatch, rows=None):
    rec = _Recorder(rows=rows)
    monkeypatch.setattr(j.db, "execute", rec.execute)
    monkeypatch.setattr(j.db, "query", rec.query)
    return rec


class TestLog:
    def test_schreibt_alle_felder(self, monkeypatch):
        rec = _patch(monkeypatch)
        j.log(3, "stage_done", "Spec fertig", tokens_in=100, tokens_out=250)
        assert rec.executes[-1][1] == (3, "stage_done", "Spec fertig", 100, 250)

    def test_task_id_darf_none_sein(self, monkeypatch):
        rec = _patch(monkeypatch)
        # Daemon-Ereignisse (Start, Not-Aus) hängen an keinem Task.
        j.log(None, "daemon_start", "Forge gestartet")
        assert rec.executes[-1][1][0] is None

    def test_unbekannte_art_wird_trotzdem_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        # Ein unbekannter kind darf nichts verschlucken — ein verlorenes
        # Ereignis ist schlimmer als ein unsauber benanntes.
        j.log(1, "irgendwas_neues", "Text")
        assert rec.executes[-1][1][1] == "irgendwas_neues"

    def test_defaults_sind_null_tokens(self, monkeypatch):
        rec = _patch(monkeypatch)
        j.log(1, "gate_pass")
        assert rec.executes[-1][1] == (1, "gate_pass", "", 0, 0)


class TestRecent:
    def test_limit_wird_durchgereicht(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.recent(limit=5)
        assert rec.queries[-1][1] == (5,)

    def test_sortiert_absteigend_nach_zeit(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.recent()
        assert "ORDER BY ts DESC" in rec.queries[-1][0]


class TestForTask:
    def test_filtert_nach_task(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.for_task(11)
        assert rec.queries[-1][1] == (11,)

    def test_sortiert_aufsteigend(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        j.for_task(11)
        # Für einen einzelnen Task will man den Verlauf von vorn lesen.
        assert "ORDER BY ts ASC" in rec.queries[-1][0]


class TestKinds:
    def test_bekannte_arten_sind_dokumentiert(self, monkeypatch):
        for kind in ["stage_start", "stage_done", "gate_pass", "gate_fail",
                     "merged", "reverted", "paused", "idea_added", "daemon_start"]:
            assert kind in j.KINDS
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_journal.py -v`
Expected: FAIL mit `ImportError: cannot import name 'journal' from 'forge'`

- [ ] **Step 3: Journal implementieren**

`forge/journal.py`:

```python
"""Ereignis-Log der Forge — Quelle für die Dashboard-View und für die Antwort
auf „was hast du gebaut?".

Bewusst schreibfreudig: lieber eine Zeile zu viel als eine fehlende Spur, wenn
nachts etwas schiefgeht. `KINDS` ist Dokumentation, keine Schranke — ein
unbekannter kind wird trotzdem geschrieben.
"""
import logging

from core import db

# Modul-Logger heißt bewusst _log: der Name `log` gehört der öffentlichen
# Journal-Funktion, die im Daemon ständig aufgerufen wird.
_log = logging.getLogger(__name__)

KINDS = frozenset({
    "daemon_start", "daemon_stop", "stage_start", "stage_done", "stage_failed",
    "gate_pass", "gate_fail", "merged", "reverted", "paused", "resumed",
    "idea_added", "parked", "lock_cleared",
})


def log(task_id: int | None, kind: str, message: str = "",
        tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Schreibt ein Ereignis. Fehler hier dürfen den Daemon nie stoppen."""
    if kind not in KINDS:
        _log.debug(f"Forge-Journal: unbekannte Ereignisart '{kind}' — wird trotzdem geschrieben")
    try:
        db.execute(
            "INSERT INTO forge_journal (task_id, kind, message, tokens_in, tokens_out) "
            "VALUES (%s, %s, %s, %s, %s)",
            (task_id, kind, message, tokens_in, tokens_out),
        )
    except Exception as exc:            # noqa: BLE001 — Journal darf nie der Grund für einen Abbruch sein
        _log.error(f"Forge-Journal-Schreibfehler: {exc}")


def recent(limit: int = 20) -> list[dict]:
    """Die jüngsten Ereignisse, neueste zuerst."""
    return db.query(
        "SELECT * FROM forge_journal ORDER BY ts DESC LIMIT %s",
        (limit,),
    )


def for_task(task_id: int) -> list[dict]:
    """Der vollständige Verlauf eines Tasks, von vorn."""
    return db.query(
        "SELECT * FROM forge_journal WHERE task_id=%s ORDER BY ts ASC",
        (task_id,),
    )
```

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_journal.py -v`
Expected: PASS, 9 Tests

- [ ] **Step 5: Lint und Commit**

```bash
ruff check forge/journal.py tests/test_forge_journal.py
git add forge/journal.py tests/test_forge_journal.py
git commit -m "feat(forge): Ereignis-Journal"
```

---

### Task 4: Git-Aufrufe mit Stale-Lock-Preflight

Das ist die Lehre aus dem Fund vom 2026-08-13: drei Null-Byte-Locks hatten das Repo **19 Tage unbemerkt** blockiert. Ohne diesen Preflight bleibt die Forge stumm hängen, statt den Grund zu melden.

**Files:**
- Create: `forge/gitctl.py`
- Test: `tests/test_forge_gitctl.py`

**Interfaces:**
- Consumes: `forge.MANTIS_REPO`
- Produces:
  - `forge.gitctl.LOCK_PATHS: tuple[str, ...]`
  - `forge.gitctl.STALE_AFTER_SECONDS: int`
  - `forge.gitctl.git_process_running() -> bool`
  - `forge.gitctl.stale_locks(repo: Path, now: float | None = None) -> list[Path]`
  - `forge.gitctl.clear_stale_locks(repo: Path) -> list[Path]`
  - `forge.gitctl.run(*args: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess`

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_gitctl.py`:

```python
"""Unit-Tests für den git-Wrapper der Forge, insbesondere den Stale-Lock-Preflight.

Hintergrund: am 2026-07-25 blockierten drei Null-Byte-Locks das Mantis-Repo
19 Tage lang unbemerkt. Diese Tests halten die Erkennung fest."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time

from forge import gitctl


def _repo_mit_lock(tmp_path, name="index.lock", alter_sekunden=0):
    """Baut ein Verzeichnis mit .git/<name> und setzt dessen Alter."""
    git_dir = tmp_path / ".git"
    (git_dir / "objects").mkdir(parents=True, exist_ok=True)
    lock = git_dir / name
    lock.write_text("")
    wann = time.time() - alter_sekunden
    os.utime(lock, (wann, wann))
    return tmp_path, lock


class TestStaleLocks:
    def test_alter_lock_ohne_git_prozess_ist_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=3600)
        assert gitctl.stale_locks(repo) == [lock]

    def test_frischer_lock_ist_nicht_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, _ = _repo_mit_lock(tmp_path, alter_sekunden=5)
        # Ein gerade angelegter Lock gehört vermutlich zu einer Operation,
        # die noch läuft — nur der Prozess ist noch nicht sichtbar.
        assert gitctl.stale_locks(repo) == []

    def test_laufender_git_prozess_macht_nichts_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: True)
        repo, _ = _repo_mit_lock(tmp_path, alter_sekunden=99999)
        assert gitctl.stale_locks(repo) == []

    def test_head_lock_wird_erkannt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, name="HEAD.lock", alter_sekunden=3600)
        assert gitctl.stale_locks(repo) == [lock]

    def test_maintenance_lock_wird_erkannt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        lock = git_dir / "maintenance.lock"
        lock.write_text("")
        wann = time.time() - 3600
        os.utime(lock, (wann, wann))
        assert gitctl.stale_locks(tmp_path) == [lock]

    def test_ohne_locks_leere_liste(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        assert gitctl.stale_locks(tmp_path) == []


class TestClearStaleLocks:
    def test_entfernt_und_meldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=3600)
        entfernt = gitctl.clear_stale_locks(repo)
        assert entfernt == [lock]
        assert not lock.exists()

    def test_laesst_frische_locks_liegen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitctl, "git_process_running", lambda: False)
        repo, lock = _repo_mit_lock(tmp_path, alter_sekunden=5)
        assert gitctl.clear_stale_locks(repo) == []
        assert lock.exists()


class TestSchwelle:
    def test_schwelle_ist_zehn_minuten(self):
        assert gitctl.STALE_AFTER_SECONDS == 600
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_gitctl.py -v`
Expected: FAIL mit `ImportError: cannot import name 'gitctl' from 'forge'`

- [ ] **Step 3: gitctl implementieren**

`forge/gitctl.py`:

```python
"""Jeder git-Aufruf der Forge läuft hier durch — mit Stale-Lock-Preflight davor.

Am 2026-07-25 starb im Mantis-Repo ein git-Prozess mitten in einer Operation und
hinterließ index.lock, HEAD.lock und objects/maintenance.lock (alle 0 Bytes).
Das Repo war danach 19 Tage schreibgeblockt, ohne dass es jemandem auffiel. Ein
Daemon, der so etwas nicht erkennt, hängt still — und still hängen ist die
schlechteste Betriebsart, die ein autonomer Loop haben kann.
"""
import logging
import os
import subprocess
import time
from pathlib import Path

from forge import MANTIS_REPO

log = logging.getLogger(__name__)

# Locks relativ zum .git-Verzeichnis.
LOCK_PATHS = ("index.lock", "HEAD.lock", "objects/maintenance.lock")

# Jünger als das? Dann gehört der Lock vermutlich zu einer Operation, die gerade
# anläuft, auch wenn noch kein Prozess sichtbar ist. Lieber warten als kaputt machen.
STALE_AFTER_SECONDS = 600


def git_process_running() -> bool:
    """Läuft irgendwo auf dem Rechner ein git-Prozess?

    Bewusst grob: pgrep kann einen Prozess nicht einem Repo zuordnen. Ein
    falsches Ja bedeutet nur, dass wir einen Lock stehen lassen — das ist die
    sichere Richtung.
    """
    try:
        ergebnis = subprocess.run(["pgrep", "-x", "git"], capture_output=True, timeout=5)
        return ergebnis.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True     # im Zweifel annehmen, dass einer läuft


def stale_locks(repo: Path, now: float | None = None) -> list[Path]:
    """Lock-Dateien, die niemandem mehr gehören."""
    if git_process_running():
        return []
    jetzt = now if now is not None else time.time()
    gefunden = []
    for rel in LOCK_PATHS:
        lock = Path(repo) / ".git" / rel
        if lock.exists() and (jetzt - os.path.getmtime(lock)) > STALE_AFTER_SECONDS:
            gefunden.append(lock)
    return gefunden


def clear_stale_locks(repo: Path) -> list[Path]:
    """Entfernt verwaiste Locks und gibt zurück, welche es waren."""
    entfernt = []
    for lock in stale_locks(repo):
        try:
            lock.unlink()
            entfernt.append(lock)
            log.warning(f"Forge: verwaisten git-Lock entfernt: {lock}")
        except OSError as exc:
            log.error(f"Forge: konnte Lock nicht entfernen ({lock}): {exc}")
    return entfernt


def run(*args: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """Führt einen git-Befehl aus, nach Lock-Preflight. Wirft nicht — der
    Aufrufer entscheidet anhand von returncode und stderr."""
    arbeitsverzeichnis = Path(cwd) if cwd is not None else MANTIS_REPO
    clear_stale_locks(arbeitsverzeichnis)
    return subprocess.run(
        ["git", *args],
        cwd=str(arbeitsverzeichnis),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
```

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_gitctl.py -v`
Expected: PASS, 9 Tests

- [ ] **Step 5: Gegen das echte Repo gegenprüfen**

Run: `python -c "from pathlib import Path; from forge import gitctl, MANTIS_REPO; print(gitctl.stale_locks(MANTIS_REPO)); print(gitctl.run('rev-parse','--short','HEAD').stdout.strip())"`
Expected: `[]` und ein kurzer Commit-Hash

- [ ] **Step 6: Lint und Commit**

```bash
ruff check forge/gitctl.py tests/test_forge_gitctl.py
git add forge/gitctl.py tests/test_forge_gitctl.py
git commit -m "feat(forge): git-Wrapper mit Stale-Lock-Preflight"
```

---

### Task 5: Worktree-Verwaltung

**Files:**
- Create: `forge/worktree.py`
- Test: `tests/test_forge_worktree.py`

**Interfaces:**
- Consumes: `forge.FORGE_ROOT`, `forge.MANTIS_REPO`, `forge.gitctl.run`
- Produces:
  - `forge.worktree.branch_for(task_id: int) -> str`
  - `forge.worktree.path_for(task_id: int) -> Path`
  - `forge.worktree.create(task_id: int, base: str = "main", repo: Path | None = None) -> Path`
  - `forge.worktree.remove(task_id: int, repo: Path | None = None) -> bool`
  - `forge.worktree.exists(task_id: int) -> bool`

Die Tests laufen hier gegen ein echtes temporäres git-Repo. `git worktree` sinnvoll zu stubben hieße, git nachzubauen — und genau die Interaktion mit git ist das, was schiefgehen kann.

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_worktree.py`:

```python
"""Tests für die Worktree-Verwaltung — gegen ein echtes temporäres git-Repo.

Kein Netz, keine Mantis-DB: `git init` in tmp_path, ein Commit, fertig. git zu
stubben würde genau die Interaktion wegabstrahieren, die hier schiefgehen kann."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import subprocess

import pytest

from forge import worktree as wt


@pytest.fixture
def repo(tmp_path):
    """Ein minimales git-Repo mit einem Commit auf 'main'."""
    ort = tmp_path / "repo"
    ort.mkdir()
    def g(*args):
        subprocess.run(["git", *args], cwd=str(ort), check=True, capture_output=True)
    g("init", "-b", "main")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "Test")
    (ort / "README.md").write_text("hallo\n")
    g("add", "README.md")
    g("commit", "-m", "erster Commit")
    return ort


@pytest.fixture
def forge_root(tmp_path, monkeypatch):
    """Lenkt die Worktree-Wurzel in tmp_path, damit ~/Mantis-forge unberührt bleibt."""
    wurzel = tmp_path / "forge-worktrees"
    monkeypatch.setattr(wt, "FORGE_ROOT", wurzel)
    return wurzel


class TestNamen:
    def test_branch_name_ist_vorhersehbar(self):
        assert wt.branch_for(7) == "forge/task-7"

    def test_pfad_liegt_unter_der_wurzel(self, forge_root):
        assert wt.path_for(7) == forge_root / "task-7"

    def test_pfad_liegt_nicht_im_repo(self, forge_root):
        # Sonst würden Linter, Tests und Suchen die Worktrees mit einsammeln.
        from forge import MANTIS_REPO
        assert MANTIS_REPO not in wt.path_for(7).parents


class TestCreate:
    def test_legt_worktree_mit_branch_an(self, repo, forge_root):
        pfad = wt.create(3, base="main", repo=repo)
        assert pfad.is_dir()
        assert (pfad / "README.md").read_text() == "hallo\n"

    def test_branch_existiert_danach(self, repo, forge_root):
        wt.create(3, base="main", repo=repo)
        zweige = subprocess.run(
            ["git", "branch", "--list", "forge/task-3"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout
        assert "forge/task-3" in zweige

    def test_zweiter_aufruf_liefert_denselben_pfad(self, repo, forge_root):
        # Wiederaufsetzen nach Absturz darf nicht an einem bestehenden
        # Worktree scheitern.
        erster = wt.create(3, base="main", repo=repo)
        zweiter = wt.create(3, base="main", repo=repo)
        assert erster == zweiter
        assert zweiter.is_dir()

    def test_hauptcheckout_bleibt_auf_main(self, repo, forge_root):
        wt.create(3, base="main", repo=repo)
        aktuell = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert aktuell == "main"


class TestExists:
    def test_false_wenn_nichts_da(self, forge_root):
        assert wt.exists(99) is False

    def test_true_nach_create(self, repo, forge_root):
        wt.create(4, base="main", repo=repo)
        assert wt.exists(4) is True


class TestRemove:
    def test_entfernt_verzeichnis(self, repo, forge_root):
        pfad = wt.create(5, base="main", repo=repo)
        assert wt.remove(5, repo=repo) is True
        assert not pfad.exists()

    def test_remove_ohne_worktree_ist_harmlos(self, repo, forge_root):
        assert wt.remove(98, repo=repo) is False

    def test_branch_bleibt_nach_remove_erhalten(self, repo, forge_root):
        # Der Branch ist die Arbeit. Ein geparkter Task muss nachvollziehbar
        # bleiben, auch wenn der Worktree weg ist.
        wt.create(6, base="main", repo=repo)
        wt.remove(6, repo=repo)
        zweige = subprocess.run(
            ["git", "branch", "--list", "forge/task-6"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout
        assert "forge/task-6" in zweige
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_worktree.py -v`
Expected: FAIL mit `ImportError: cannot import name 'worktree' from 'forge'`

- [ ] **Step 3: Worktree-Verwaltung implementieren**

`forge/worktree.py`:

```python
"""Isolierte Arbeitskopien pro Task.

Im Haupt-Checkout (~/Mantis) wird nie gearbeitet — dort läuft der produktive
Mantis. Jeder Task bekommt einen eigenen Worktree unter ~/Mantis-forge/task-<id>
mit eigenem Branch. Der Branch überlebt das Entfernen des Worktrees, damit die
Arbeit eines geparkten Tasks nachvollziehbar bleibt.
"""
import logging
import shutil
from pathlib import Path

from forge import FORGE_ROOT, MANTIS_REPO
from forge import gitctl

log = logging.getLogger(__name__)


def branch_for(task_id: int) -> str:
    return f"forge/task-{task_id}"


def path_for(task_id: int) -> Path:
    return FORGE_ROOT / f"task-{task_id}"


def exists(task_id: int) -> bool:
    return path_for(task_id).is_dir()


def create(task_id: int, base: str = "main", repo: Path | None = None) -> Path:
    """Legt den Worktree an — oder gibt den bestehenden zurück.

    Idempotent, weil der Daemon nach einem Absturz auf derselben Stufe wieder
    aufsetzt und dann auf einen bereits vorhandenen Worktree trifft.
    """
    ziel = path_for(task_id)
    if ziel.is_dir():
        return ziel

    ziel.parent.mkdir(parents=True, exist_ok=True)
    zweig = branch_for(task_id)
    quelle = Path(repo) if repo is not None else MANTIS_REPO

    ergebnis = gitctl.run(
        "worktree", "add", "-b", zweig, str(ziel), base, cwd=quelle,
    )
    if ergebnis.returncode != 0:
        # Zweiter Versuch ohne -b: der Branch kann aus einem früheren Anlauf
        # noch existieren, während der Worktree schon entfernt wurde.
        ergebnis = gitctl.run("worktree", "add", str(ziel), zweig, cwd=quelle)
    if ergebnis.returncode != 0:
        raise RuntimeError(f"worktree add fehlgeschlagen: {ergebnis.stderr.strip()}")

    log.info(f"Forge: Worktree für Task {task_id} unter {ziel}")
    return ziel


def remove(task_id: int, repo: Path | None = None) -> bool:
    """Entfernt den Worktree. Der Branch bleibt erhalten."""
    ziel = path_for(task_id)
    if not ziel.exists():
        return False

    quelle = Path(repo) if repo is not None else MANTIS_REPO
    ergebnis = gitctl.run("worktree", "remove", "--force", str(ziel), cwd=quelle)
    if ergebnis.returncode != 0:
        # git weigert sich gelegentlich (z.B. bei fremden Dateien im Baum).
        # Dann von Hand wegräumen und gits Registrierung nachziehen.
        shutil.rmtree(ziel, ignore_errors=True)
        gitctl.run("worktree", "prune", cwd=quelle)
    return not ziel.exists()
```

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_worktree.py -v`
Expected: PASS, 12 Tests

- [ ] **Step 5: Sicherstellen, dass die Worktree-Wurzel nicht ins Repo rutscht**

Run: `grep -n "Mantis-forge" .gitignore || echo "Mantis-forge/" >> .gitignore`
Expected: Danach steht `Mantis-forge/` in `.gitignore` (Gürtel und Hosenträger — die Wurzel liegt ohnehin außerhalb des Repos)

- [ ] **Step 6: Lint und Commit**

```bash
ruff check forge/worktree.py tests/test_forge_worktree.py
git add forge/worktree.py tests/test_forge_worktree.py .gitignore
git commit -m "feat(forge): isolierte Worktrees pro Task"
```

---

### Task 6: Claude-Runner

**Files:**
- Create: `forge/runner.py`
- Create: `tests/fixtures/claude_stream_success.jsonl` *(aufgenommen, nicht erfunden)*
- Test: `tests/test_forge_runner.py`

**Interfaces:**
- Consumes: nichts aus der Forge
- Produces:
  - `forge.runner.RunResult` — Dataclass mit `ok: bool`, `text: str`, `tokens_in: int`, `tokens_out: int`, `error: str | None`, `rate_limited: bool`
  - `forge.runner.parse_stream(lines: Iterable[str]) -> RunResult`
  - `forge.runner.run(prompt: str, cwd: Path, timeout: int = 1800) -> RunResult`

**Wichtig:** Das genaue `stream-json`-Format wird **aufgenommen, nicht geraten** (Spec §13). Schritt 1 nimmt eine echte Ausgabe auf; die Tests laufen gegen diese Aufnahme.

- [ ] **Step 1: Echte Ausgabe aufnehmen**

```bash
mkdir -p tests/fixtures
claude -p "Antworte nur mit dem Wort: hallo" --output-format stream-json --verbose > tests/fixtures/claude_stream_success.jsonl
```

Erwartet: mehrere JSON-Zeilen, die letzte mit `"type":"result"`. Prüfen:

```bash
python -c "import json,sys; [print(json.loads(l).get('type'), list(json.loads(l).keys())) for l in open('tests/fixtures/claude_stream_success.jsonl') if l.strip()]"
```

**Wenn der Befehl `--verbose` verlangt oder ein anderes Format liefert:** die Aufnahme ist die Wahrheit. Feldnamen in Schritt 3 an die Aufnahme anpassen, nicht umgekehrt.

**Bereits am 2026-08-13 an einem echten Lauf verifiziert** (CLI 2.1.126, Fehlerfall): das Result-Event enthält `result`, `is_error`, `subtype`, `usage`, `api_error_status`, `total_cost_usd`, `modelUsage`. Drei Befunde daraus, die in die Implementierung gehören:

1. **`is_error` ist die Autorität, nicht `subtype`.** Der beobachtete Lauf hatte `subtype: "success"` bei `is_error: true`. Wer auf `subtype` prüft, hält Fehlläufe für Erfolge. `parse_stream` prüft korrekt `is_error`.
2. **`usage` enthält mehr als `input_tokens`/`output_tokens`** — zusätzlich `cache_creation_input_tokens` und `cache_read_input_tokens`. Für Plan 1 genügen die beiden Hauptwerte; die Budget-Rechnung in Plan 3 muss die Cache-Felder mitzählen, sonst rechnet sie zu niedrig.
3. **`api_error_status`** ist im Fehlerfall gesetzt — in Plan 3 der verlässlichere Weg zur Rate-Limit-Erkennung als Textmarker im Ergebnis.

Was die Aufnahme noch **nicht** belegt: ein erfolgreicher Lauf mit `tokens_in > 0`. Genau dafür ist dieser Schritt da.

- [ ] **Step 2: Test schreiben, der fehlschlägt**

`tests/test_forge_runner.py`:

```python
"""Tests für den Claude-Runner. Die Erfolgs-Fixture ist eine echte Aufnahme von
`claude -p ... --output-format stream-json`, keine erfundene Struktur."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from pathlib import Path

from forge import runner

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream_success.jsonl"


class TestParseEchteAufnahme:
    def test_erfolgreicher_lauf_ist_ok(self):
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.ok is True

    def test_text_ist_nicht_leer(self):
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.text.strip() != ""

    def test_tokens_werden_gezaehlt(self):
        # Ohne Verbrauchszahlen kann budget.py in Plan 3 nicht rechnen.
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.tokens_in > 0
        assert ergebnis.tokens_out > 0


class TestParseFehlerfaelle:
    def test_fehlerhafter_lauf_ist_nicht_ok(self):
        zeilen = [json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "boom",
        })]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is False
        assert ergebnis.error is not None

    def test_kaputte_json_zeilen_werden_uebersprungen(self):
        # Die CLI mischt gelegentlich Nicht-JSON-Zeilen dazwischen. Eine davon
        # darf nicht den gesamten Lauf als Fehlschlag erscheinen lassen.
        zeilen = [
            "kein json",
            "",
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "fertig",
                        "usage": {"input_tokens": 10, "output_tokens": 20}}),
        ]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is True
        assert ergebnis.text == "fertig"

    def test_ohne_result_zeile_ist_es_ein_fehler(self):
        # Abgeschnittener Stream = abgebrochener Lauf.
        zeilen = [json.dumps({"type": "assistant", "message": {"content": []}})]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is False
        assert "kein Ergebnis" in ergebnis.error

    def test_leerer_stream_ist_ein_fehler(self):
        ergebnis = runner.parse_stream([])
        assert ergebnis.ok is False

    def test_fehlende_usage_ergibt_null_tokens(self):
        zeilen = [json.dumps({"type": "result", "subtype": "success",
                              "is_error": False, "result": "ok"})]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is True
        assert ergebnis.tokens_in == 0


class TestRateLimitErkennung:
    def test_rate_limit_wird_als_solches_markiert(self):
        # Plan 3 verlässt sich auf dieses Flag, um bis zum Reset zu schlafen,
        # statt den Task zu parken.
        zeilen = [json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": "Claude AI usage limit reached|1755100000",
        })]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.rate_limited is True

    def test_normaler_fehler_ist_kein_rate_limit(self):
        zeilen = [json.dumps({"type": "result", "subtype": "error_during_execution",
                              "is_error": True, "result": "Datei nicht gefunden"})]
        assert runner.parse_stream(zeilen).rate_limited is False
```

- [ ] **Step 3: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_runner.py -v`
Expected: FAIL mit `ImportError: cannot import name 'runner' from 'forge'`

- [ ] **Step 4: Runner implementieren**

`forge/runner.py`:

```python
"""Startet headless Claude-Code-Läufe und wertet ihre Ausgabe aus.

Ein Lauf pro Pipeline-Stufe, jeweils mit frischem Kontext. Die Übergabe
zwischen Stufen läuft über Dateien im Worktree, nicht über Gesprächsverlauf —
deshalb genügt hier ein einzelner Aufruf ohne Sitzungsverwaltung.
"""
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Textmarker, an denen ein erschöpftes Kontingent erkannt wird. Plan 3 nutzt das
# Flag, um bis zum Reset zu schlafen, statt den Task zu parken.
_RATE_LIMIT_MARKER = ("usage limit reached", "rate limit", "rate_limit")


@dataclass
class RunResult:
    ok: bool
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    rate_limited: bool = False
    raw: list[dict] = field(default_factory=list)


def _ist_rate_limit(text: str) -> bool:
    klein = (text or "").lower()
    return any(marker in klein for marker in _RATE_LIMIT_MARKER)


def parse_stream(lines: Iterable[str]) -> RunResult:
    """Wertet die stream-json-Ausgabe aus.

    Robust gegen Nicht-JSON-Zeilen: die CLI mischt gelegentlich Klartext dazu,
    und daran darf ein sonst erfolgreicher Lauf nicht scheitern.
    """
    ereignisse: list[dict] = []
    for zeile in lines:
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            ereignisse.append(json.loads(zeile))
        except json.JSONDecodeError:
            continue

    schluss = next((e for e in reversed(ereignisse) if e.get("type") == "result"), None)
    if schluss is None:
        return RunResult(ok=False, error="kein Ergebnis im Stream (Lauf abgebrochen?)", raw=ereignisse)

    text = schluss.get("result") or ""
    verbrauch = schluss.get("usage") or {}
    tokens_in = int(verbrauch.get("input_tokens") or 0)
    tokens_out = int(verbrauch.get("output_tokens") or 0)

    if schluss.get("is_error"):
        return RunResult(
            ok=False, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            error=text or schluss.get("subtype") or "unbekannter Fehler",
            rate_limited=_ist_rate_limit(text), raw=ereignisse,
        )

    return RunResult(ok=True, text=text, tokens_in=tokens_in, tokens_out=tokens_out, raw=ereignisse)


def run(prompt: str, cwd: Path, timeout: int = 1800) -> RunResult:
    """Führt einen headless Lauf im angegebenen Worktree aus."""
    befehl = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    try:
        fertig = subprocess.run(
            # stdin MUSS abgeklemmt werden: ohne DEVNULL wartet die CLI drei Sekunden
            # auf Eingabe und schreibt eine Warnung — pro Stufe, bei jedem Lauf.
            # Live gemessen am 2026-08-13.
            befehl, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, error=f"Zeitüberschreitung nach {timeout}s")
    except OSError as exc:
        return RunResult(ok=False, error=f"claude nicht startbar: {exc}")

    ergebnis = parse_stream(fertig.stdout.splitlines())
    if not ergebnis.ok and fertig.stderr.strip():
        ergebnis.error = f"{ergebnis.error} | stderr: {fertig.stderr.strip()[:500]}"
        if _ist_rate_limit(fertig.stderr):
            ergebnis.rate_limited = True
    return ergebnis
```

- [ ] **Step 5: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_runner.py -v`
Expected: PASS, 10 Tests

Schlägt einer der drei `TestParseEchteAufnahme`-Tests fehl, weichen die Feldnamen der echten Aufnahme von `result`/`is_error`/`usage.input_tokens` ab. Dann `parse_stream` an die Aufnahme anpassen — die Aufnahme hat recht.

- [ ] **Step 6: Lint und Commit**

```bash
ruff check forge/runner.py tests/test_forge_runner.py
git add forge/runner.py tests/test_forge_runner.py tests/fixtures/claude_stream_success.jsonl
git commit -m "feat(forge): headless Claude-Runner mit stream-json-Auswertung"
```

---

### Task 7: Daemon und launchd

Am Ende dieses Tasks läuft die Kette einmal komplett durch: Task aus der Queue → Worktree → ein Claude-Lauf → Journal → Task geparkt. Gemerged wird nichts; das ist Plan 3.

**Files:**
- Create: `forge/daemon.py`
- Create: `~/Library/LaunchAgents/com.mantis.forge.plist`
- Test: `tests/test_forge_daemon.py`

**Interfaces:**
- Consumes: `forge.queue`, `forge.journal`, `forge.worktree`, `forge.runner`, `forge.models`
- Produces:
  - `forge.daemon.STOP_FILE: Path`
  - `forge.daemon.MAX_CONSECUTIVE_FAILURES: int`
  - `forge.daemon.interactive_claude_running() -> bool`
  - `forge.daemon.should_run(failures: int) -> tuple[bool, str]`
  - `forge.daemon.tick() -> str` — gibt zurück, was passiert ist (für Tests und Journal)
  - `forge.daemon.main() -> None`

- [ ] **Step 1: Test schreiben, der fehlschlägt**

`tests/test_forge_daemon.py`:

```python
"""Tests für die Entscheidungslogik des Forge-Daemons.

Geprüft wird, WANN gearbeitet wird — nicht ob claude funktioniert. Alle
Außenkontakte (Queue, Worktree, Runner) sind gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import daemon as d
from forge import models as m
from forge.runner import RunResult


@pytest.fixture
def frei(monkeypatch, tmp_path):
    """Standardlage: kein Not-Aus, keine interaktive Sitzung."""
    monkeypatch.setattr(d, "STOP_FILE", tmp_path / "kein-stop")
    monkeypatch.setattr(d, "interactive_claude_running", lambda: False)


class TestShouldRun:
    def test_normalfall_laeuft(self, frei):
        laeuft, _ = d.should_run(failures=0)
        assert laeuft is True

    def test_not_aus_datei_stoppt(self, monkeypatch, tmp_path):
        stop = tmp_path / "stop"
        stop.write_text("")
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "interactive_claude_running", lambda: False)
        laeuft, grund = d.should_run(failures=0)
        assert laeuft is False
        assert "Not-Aus" in grund

    def test_interaktive_sitzung_pausiert(self, monkeypatch, tmp_path):
        # Sonst konkurrieren Timo und die Forge um dasselbe Limit und um die 16 GB.
        monkeypatch.setattr(d, "STOP_FILE", tmp_path / "kein-stop")
        monkeypatch.setattr(d, "interactive_claude_running", lambda: True)
        laeuft, grund = d.should_run(failures=0)
        assert laeuft is False
        assert "interaktive" in grund.lower()

    def test_fehler_spirale_stoppt(self, frei):
        laeuft, grund = d.should_run(failures=d.MAX_CONSECUTIVE_FAILURES)
        assert laeuft is False
        assert "Fehlschläge" in grund

    def test_schwelle_ist_drei(self):
        assert d.MAX_CONSECUTIVE_FAILURES == 3

    def test_zwei_fehler_stoppen_noch_nicht(self, frei):
        laeuft, _ = d.should_run(failures=2)
        assert laeuft is True


class TestFehlerSpiraleUeberlebtNeustart:
    def test_spirale_setzt_die_not_aus_datei(self, monkeypatch, tmp_path):
        # Der launchd-Job läuft mit KeepAlive=true. Ohne diese Datei würde der
        # Daemon 30s nach dem Selbst-Stopp mit failures=0 neu starten und
        # dieselben Fehlläufe erneut verbrennen — die Bremse wäre keine.
        stop = tmp_path / "stop"
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "interactive_claude_running", lambda: False)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d, "tick", lambda: "fehler")
        d.main()
        assert stop.exists()

    def test_nach_der_spirale_laeuft_nichts_mehr(self, monkeypatch, tmp_path):
        stop = tmp_path / "stop"
        stop.write_text("Fehler-Spirale\n")
        monkeypatch.setattr(d, "STOP_FILE", stop)
        monkeypatch.setattr(d, "interactive_claude_running", lambda: False)
        laeuft, grund = d.should_run(failures=0)
        assert laeuft is False
        assert "Not-Aus" in grund


class TestTick:
    def test_leere_queue_meldet_leerlauf(self, monkeypatch, frei):
        monkeypatch.setattr(d.queue, "claim_next", lambda: None)
        assert d.tick() == "leerlauf"

    def test_erfolgreicher_lauf_parkt_den_task(self, monkeypatch, frei, tmp_path):
        # Plan 1 merged noch nicht — jeder Task endet als Trockenlauf geparkt.
        aufrufe = {}
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 1, "title": "Test", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=True, text="fertig",
                                                                        tokens_in=5, tokens_out=7))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: aufrufe.setdefault("park", (tid, reason)))
        assert d.tick() == "trockenlauf"
        assert aufrufe["park"][0] == 1

    def test_fehlgeschlagener_lauf_parkt_mit_grund(self, monkeypatch, frei, tmp_path):
        aufrufe = {}
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 2, "title": "Test", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=False, error="kaputt"))
        monkeypatch.setattr(d.journal, "log", lambda *a, **kw: None)
        monkeypatch.setattr(d.queue, "park",
                            lambda tid, current, reason: aufrufe.setdefault("park", (tid, reason)))
        assert d.tick() == "fehler"
        assert "kaputt" in aufrufe["park"][1]

    def test_journal_bekommt_die_tokens(self, monkeypatch, frei, tmp_path):
        # Ohne diese Zahlen kann Plan 3 kein Budget führen.
        gemerkt = []
        monkeypatch.setattr(d.queue, "claim_next",
                            lambda: {"id": 3, "title": "T", "description": "", "state": m.SPECCING})
        monkeypatch.setattr(d.worktree, "create", lambda tid, **kw: tmp_path)
        monkeypatch.setattr(d.runner, "run",
                            lambda prompt, cwd, timeout=1800: RunResult(ok=True, text="x",
                                                                        tokens_in=11, tokens_out=22))
        monkeypatch.setattr(d.queue, "park", lambda tid, current, reason: True)
        monkeypatch.setattr(d.journal, "log",
                            lambda *a, **kw: gemerkt.append((kw.get("tokens_in"), kw.get("tokens_out"))))
        d.tick()
        assert (11, 22) in gemerkt


class TestInteractiveDetection:
    def test_eigener_daemon_zaehlt_nicht_als_interaktiv(self, monkeypatch):
        # Der Forge-Prozess startet selbst `claude`-Kindprozesse. Würden die als
        # interaktive Sitzung gelten, würde sich die Forge selbst aussperren.
        class _Fertig:
            returncode = 0
            stdout = f"{os.getpid()}\n"
        monkeypatch.setattr(d.subprocess, "run", lambda *a, **kw: _Fertig())
        assert d.interactive_claude_running() is False
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `python -m pytest tests/test_forge_daemon.py -v`
Expected: FAIL mit `ImportError: cannot import name 'daemon' from 'forge'`

- [ ] **Step 3: Daemon implementieren**

`forge/daemon.py`:

```python
"""Hauptschleife der Forge.

Plan 1 (Fundament): ein Task, ein Worktree, ein Claude-Lauf, Journal, parken.
Gemerged wird noch nichts — die Pipeline kommt in Plan 2, Merge und
Neustart-Etikette in Plan 3.

Der Daemon hält sich an drei Bremsen: Not-Aus-Datei, laufende interaktive
Claude-Sitzung, und drei Fehlschläge in Folge.
"""
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from core import db

from forge import journal, queue, runner, worktree

log = logging.getLogger(__name__)

STOP_FILE = Path.home() / ".mantis-forge-stop"
MAX_CONSECUTIVE_FAILURES = 3
IDLE_SLEEP_SECONDS = 60
BLOCKED_SLEEP_SECONDS = 300


def interactive_claude_running() -> bool:
    """Läuft eine interaktive Claude-Code-Sitzung von Timo?

    Die eigenen Kindprozesse zählen nicht — sonst sperrt sich die Forge selbst aus.
    """
    try:
        fertig = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if fertig.returncode != 0:
        return False

    eigene = {os.getpid()}
    for zeile in fertig.stdout.split():
        try:
            pid = int(zeile)
        except ValueError:
            continue
        if pid in eigene or _ist_kind_von_uns(pid):
            continue
        return True
    return False


def _ist_kind_von_uns(pid: int) -> bool:
    """Hängt der Prozess unter diesem Daemon?"""
    try:
        fertig = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
        return int(fertig.stdout.strip() or -1) == os.getpid()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def should_run(failures: int) -> tuple[bool, str]:
    """Darf gerade gearbeitet werden? Zweiter Rückgabewert ist der Grund."""
    if STOP_FILE.exists():
        return False, f"Not-Aus aktiv ({STOP_FILE})"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return False, f"{failures} Fehlschläge in Folge — Daemon hält an"
    if interactive_claude_running():
        return False, "interaktive Claude-Sitzung läuft"
    return True, "frei"


def _trockenlauf_prompt(task: dict) -> str:
    """Plan-1-Prompt: nur orientieren, nichts ändern."""
    return (
        "Du arbeitest in einem isolierten git-Worktree des Mantis-Projekts.\n"
        f"Anstehende Aufgabe: {task['title']}\n"
        f"{task.get('description') or ''}\n\n"
        "Das ist ein Trockenlauf. Ändere KEINE Dateien und committe nichts. "
        "Lies dich ein und antworte in höchstens 10 Zeilen: welche Dateien wären "
        "für diese Aufgabe relevant, und wo liegt die größte Unsicherheit?"
    )


def tick() -> str:
    """Ein Durchlauf. Rückgabe: 'leerlauf' | 'trockenlauf' | 'fehler'."""
    task = queue.claim_next()
    if task is None:
        return "leerlauf"

    task_id = task["id"]
    journal.log(task_id, "stage_start", f"Trockenlauf für: {task['title']}")

    baum = worktree.create(task_id)
    ergebnis = runner.run(_trockenlauf_prompt(task), cwd=baum)

    journal.log(
        task_id,
        "stage_done" if ergebnis.ok else "stage_failed",
        (ergebnis.text or ergebnis.error or "")[:2000],
        tokens_in=ergebnis.tokens_in,
        tokens_out=ergebnis.tokens_out,
    )

    if not ergebnis.ok:
        queue.park(task_id, current=task["state"], reason=f"Trockenlauf fehlgeschlagen: {ergebnis.error}")
        return "fehler"

    queue.park(task_id, current=task["state"],
               reason="Plan-1-Trockenlauf abgeschlossen — Pipeline folgt in Plan 2")
    return "trockenlauf"


def main() -> None:
    """launchd-Einstieg. Läuft bis zum Not-Aus oder bis zur Fehler-Spirale."""
    # stream=sys.stdout explizit: ohne das geht alles nach stderr, landet also in
    # mantis_forge_err.log statt im out.log, das die Verifikation unten (Step 8/9)
    # tailt — sonst kann diese Verifikation nie grün werden (Finding I3).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    db.init_pool()
    journal.log(None, "daemon_start", "Forge gestartet")
    log.info("Forge-Daemon gestartet")

    failures = 0
    while True:
        erlaubt, grund = should_run(failures)
        if not erlaubt:
            log.info(f"Forge pausiert: {grund}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # Die Bremse MUSS den launchd-Neustart überleben. Der Job läuft mit
                # KeepAlive=true; ein bloßes return würde 30s später neu starten, den
                # Zähler auf 0 setzen und dieselben drei Fehlläufe erneut verbrennen —
                # eine Endlosschleife statt einer Bremse. Die Not-Aus-Datei ist der
                # einzige Zustand, den ein Neustart nicht vergisst.
                STOP_FILE.write_text(f"Fehler-Spirale: {grund}\n")
                journal.log(None, "daemon_stop", f"{grund} — Not-Aus gesetzt, Freigabe durch Timo")
                return
            time.sleep(BLOCKED_SLEEP_SECONDS)
            continue

        try:
            ergebnis = tick()
        except Exception as exc:            # noqa: BLE001 — ein Task darf den Daemon nicht töten
            log.exception("Forge-Tick abgestürzt")
            journal.log(None, "stage_failed", f"Tick-Absturz: {exc}")
            ergebnis = "fehler"

        failures = failures + 1 if ergebnis == "fehler" else 0
        if ergebnis == "leerlauf":
            time.sleep(IDLE_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `python -m pytest tests/test_forge_daemon.py -v`
Expected: PASS, 13 Tests

- [ ] **Step 5: Gesamte Test-Suite laufen lassen**

Run: `python -m pytest tests/ -q`
Expected: Alle Tests grün, inklusive der bisherigen — die Forge fasst nichts Bestehendes an außer der `MIGRATIONS`-Liste

- [ ] **Step 6: Kette einmal von Hand durchspielen**

```bash
python -c "
from core import db
from forge import queue
db.init_pool()
print('Task-ID:', queue.enqueue('Trockenlauf-Probe', 'Nur zum Prüfen der Kette', source='timo', priority=99))
"
python -c "
from forge import daemon
from core import db
db.init_pool()
print('Ergebnis:', daemon.tick())
"
```

Expected: Zweiter Aufruf gibt `Ergebnis: trockenlauf` aus. Danach prüfen:

```bash
python -c "
from core import db
db.init_pool()
for z in db.query('SELECT kind, left(message,60) AS m, tokens_in, tokens_out FROM forge_journal ORDER BY ts'):
    print(z)
"
ls ~/Mantis-forge/
```

Expected: Journal enthält `stage_start` und `stage_done` mit Token-Zahlen > 0; unter `~/Mantis-forge/` liegt ein `task-<id>`-Verzeichnis.

- [ ] **Step 7: launchd-Job anlegen**

`~/Library/LaunchAgents/com.mantis.forge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mantis.forge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14</string>
        <string>-u</string>
        <string>-m</string>
        <string>forge.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/timoegersdorfer/Mantis</string>
    <!-- Ohne das bekommt ein launchd-User-Agent nur PATH=/usr/bin:/bin:/usr/sbin:/sbin —
         claude liegt aber unter ~/.local/bin. Ohne diesen Eintrag scheitert jeder
         `claude`-Aufruf mit FileNotFoundError, runner.run() liefert "claude nicht
         startbar", und jeder Task wird mit null Tokens geparkt, ohne dass es auffällt. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/timoegersdorfer/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <!-- Immer neu starten: nach einem Merge beendet sich der Daemon absichtlich
         mit Code 0, damit er mit frischem Code wieder hochkommt (ab Plan 3). -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/mantis_forge_out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mantis_forge_err.log</string>
</dict>
</plist>
```

Der Pfad ist derselbe Interpreter, den `start.sh` über `which python3.14` findet — geprüft am 2026-08-13. Weicht `which python3.14` ab, gilt der tatsächliche Wert.

`EnvironmentVariables.PATH` ist nicht optional: ohne sie sieht der Prozess nur die launchd-Grundausstattung `/usr/bin:/bin:/usr/sbin:/sbin`, `claude` liegt aber unter `~/.local/bin`. `git`, `pgrep` und `ps` liegen alle in `/usr/bin`, deshalb bleibt der Ausfall auf `claude`-Läufe beschränkt — und fällt deshalb leicht niemandem auf (gefunden und gefixt am 2026-08-13, siehe Finding C1 im Review).

- [ ] **Step 8: Job laden und beobachten**

```bash
launchctl load ~/Library/LaunchAgents/com.mantis.forge.plist
sleep 5
launchctl list | grep com.mantis.forge
tail -20 /tmp/mantis_forge_out.log
```

Expected: `launchctl list` zeigt den Job mit Exit-Code 0 oder laufender PID; das Log enthält `Forge-Daemon gestartet`.

- [ ] **Step 9: Not-Aus prüfen**

```bash
touch ~/.mantis-forge-stop
sleep 5
tail -3 /tmp/mantis_forge_out.log
```

Expected: Logzeile `Forge pausiert: Not-Aus aktiv (...)`. Danach wieder freigeben:

```bash
rm ~/.mantis-forge-stop
```

- [ ] **Step 10: Lint und Commit**

```bash
ruff check forge/ tests/test_forge_daemon.py
git add forge/daemon.py tests/test_forge_daemon.py
git commit -m "feat(forge): Daemon mit Not-Aus, Pausenlogik und launchd-Job"
```

---

## Nachtrag: Fixes aus dem Whole-Branch-Review (2026-08-13)

Eine Review vor Plan 2 fand mehrere Fundamentbrüche, die die Code-Blöcke oben
nicht mehr abbilden (diese Blöcke sind Planungs-Historie, kein Änderungslog).
Kurzfassung, Details im Fix-Report unter `.superpowers/sdd/final-fix-report.md`:

- **C1**: `EnvironmentVariables.PATH` im plist nachgetragen (siehe Task 7 oben)
  UND `forge/runner.py` löst `claude` jetzt einmalig über `shutil.which()` auf
  und meldet ein fehlendes Binary spezifisch statt generisch.
- **C2**: `daemon.tick()` parkt jetzt in einem `try/except` bevor eine Ausnahme
  weitergereicht wird; `queue.claim_next()` zählt Versuche und parkt ab drei
  automatisch; `main()` bekommt einen Backoff-Sleep auf dem Fehlerpfad.
- **I1**: `gitctl.run()` wirft nicht mehr bei `TimeoutExpired` oder wenn ein
  Lock zwischen Preflight-Prüfung und Zugriff verschwindet — synthetisches
  `CompletedProcess` statt Exception, wie der Docstring es immer schon versprach.
- **I2**: `runner.parse_stream()` crasht nicht mehr an gültigem Nicht-Objekt-JSON
  und behandelt fehlendes `is_error` als Fehlschlag, nicht als Erfolg.
- **I4**: `tick()` prüft jetzt den Rückgabewert beider `queue.park()`-Aufrufe.
- **I5**: `worktree.create()` verlangt `(ziel / ".git").exists()`, bevor ein
  bestehendes Verzeichnis als fertiger Worktree gilt.
- **I6**: `gitctl.stale_locks()` löst das git-Verzeichnis über
  `git rev-parse --git-dir` auf statt `.git/` anzunehmen.
- **I8**: `daemon.main()` ruft jetzt `db.run_migrations()`; `queue.set_state()`
  und `queue.park()` sind Compare-and-Swap (`WHERE id=%s AND state=%s`).

## Abnahme für Plan 1

Alles davon muss zutreffen, bevor Plan 2 anfängt:

- [ ] `python -m pytest tests/ -q` komplett grün
- [ ] `ruff check .` ohne Befund
- [ ] `launchctl list | grep com.mantis.forge` zeigt den Job
- [ ] Ein eingereihter Task erzeugt einen Worktree unter `~/Mantis-forge/`, einen Claude-Lauf und Journal-Einträge mit Token-Zahlen > 0
- [ ] `touch ~/.mantis-forge-stop` pausiert den Daemon nachweislich
- [ ] `~/Mantis` steht unverändert auf `main`, `git status` dort sauber

## Was Plan 1 bewusst noch nicht kann

Kein Merge, kein Neustart, keine Pipeline über eine Stufe hinaus, keine Budget-Verwaltung, kein Dashboard, kein Ideen-Generator. Der Trockenlauf-Prompt weist ausdrücklich an, nichts zu ändern — Plan 1 beweist die Kette, nicht die Entwicklungsarbeit.
