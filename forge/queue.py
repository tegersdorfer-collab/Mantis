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
