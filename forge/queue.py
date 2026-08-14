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

# Ab diesem Versuchszähler wird ein Task automatisch geparkt, egal was als
# Nächstes passiert — muss mit forge.daemon.MAX_CONSECUTIVE_FAILURES
# übereinstimmen. Sonst hält ein Task, der bei jedem Versuch abstürzt (und
# dessen park()-Aufruf selbst aus irgendeinem Grund nicht durchkommt), die
# Spitze der Queue für immer, auch wenn der Daemon selbst längst bremst.
AUTO_PARK_AFTER_ATTEMPTS = 3


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
    oberste aus der Queue — der wird dabei auf die erste Stufe gesetzt. Jeder
    zurückgegebene Task zählt als ein Versuch (siehe `_versuch_zaehlen`).
    """
    laufend = active()
    if laufend is not None:
        return _versuch_zaehlen(laufend)

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
    return _versuch_zaehlen(task)


def _versuch_zaehlen(task: dict) -> dict | None:
    """Zählt einen Bearbeitungsversuch am Task. Übersteigt er die Schwelle,
    wird der Task automatisch geparkt und None zurückgegeben, statt an den
    Aufrufer weiterzureichen — sonst könnte ein Task, der bei jedem Anlauf
    abstürzt, ohne dass tick() selbst zum Parken kommt, die Queue für immer
    blockieren."""
    versuche = (task.get("attempts") or 0) + 1
    if versuche > AUTO_PARK_AFTER_ATTEMPTS:
        park(task["id"], current=task["state"],
             reason=f"Automatisch geparkt: {versuche} Versuche ohne Erfolg")
        return None
    db.execute(
        "UPDATE forge_tasks SET attempts=%s, updated_at=NOW() WHERE id=%s",
        (versuche, task["id"]),
    )
    task["attempts"] = versuche
    return task


def set_state(task_id: int, target: str, current: str) -> bool:
    """Setzt den Zustand, sofern der Übergang erlaubt ist. Sonst False.

    Die WHERE-Klausel prüft den erwarteten Ausgangszustand mit (Compare-and-
    Swap): ohne das könnten zwei Daemonen — oder ein Handlauf neben dem
    laufenden launchd-Job — denselben Task per read-then-write doppelt claimen
    und doppelt bezahlen. `db.execute` liefert den rowcount; nur bei genau
    einer betroffenen Zeile hat DIESER Aufruf den Übergang tatsächlich vollzogen.
    """
    if not m.can_transition(current, target):
        log.warning(f"Forge: verbotener Übergang {current} → {target} (Task {task_id})")
        return False
    betroffen = db.execute(
        "UPDATE forge_tasks SET state=%s, updated_at=NOW() WHERE id=%s AND state=%s",
        (target, task_id, current),
    )
    return betroffen == 1


def park(task_id: int, current: str, reason: str) -> bool:
    """Legt den Task zur manuellen Sichtung beiseite. Der Worktree bleibt stehen.

    Ebenfalls Compare-and-Swap (siehe `set_state`) — der Rückgabewert muss sich
    darauf verlassen können, dass der Task tatsächlich geparkt wurde, nicht nur
    dass der Übergang theoretisch erlaubt gewesen wäre.
    """
    if not m.can_transition(current, m.PARKED):
        return False
    betroffen = db.execute(
        "UPDATE forge_tasks SET state=%s, parked_reason=%s, updated_at=NOW() WHERE id=%s AND state=%s",
        (m.PARKED, reason, task_id, current),
    )
    return betroffen == 1


def pause(task_id: int, reason: str, until: datetime) -> None:
    """Pausiert den Task bis `until`, ohne die Stufe zu verlassen."""
    db.execute(
        "UPDATE forge_tasks SET pause_reason=%s, paused_until=%s, updated_at=NOW() WHERE id=%s",
        (reason, until, task_id),
    )


# Nur diese Felder dürfen über setze_artefakt geschrieben werden. Der Feldname
# geht in den SQL-Text — ohne Whitelist wäre das eine Injection-Stelle mitten in
# der Datenschicht, erreichbar über einen Pfad, den ein Agent bestimmt hat.
_ARTEFAKT_FELDER = frozenset({"spec_path", "plan_path", "worktree_path", "branch"})


def setze_artefakt(task_id: int, feld: str, pfad: str) -> None:
    """Hinterlegt den Pfad eines Stufen-Artefakts."""
    if feld not in _ARTEFAKT_FELDER:
        raise ValueError(f"Unzulässiges Artefakt-Feld: {feld}")
    db.execute(f"UPDATE forge_tasks SET {feld}=%s, updated_at=NOW() WHERE id=%s", (pfad, task_id))


def zaehle_fixrunde(task_id: int) -> int:
    """Erhöht den Fix-Runden-Zähler und gibt den neuen Stand zurück."""
    zeile = db.query_one(
        "UPDATE forge_tasks SET refusals=refusals+1, updated_at=NOW() "
        "WHERE id=%s RETURNING refusals",
        (task_id,),
    )
    return int(zeile["refusals"]) if zeile else 0
