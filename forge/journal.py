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
