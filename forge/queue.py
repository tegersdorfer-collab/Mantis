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

# Ab diesem Fehlschlag-Zähler wird ein Task automatisch geparkt, egal was als
# Nächstes passiert — muss mit forge.daemon.MAX_CONSECUTIVE_FAILURES
# übereinstimmen. Sonst hält ein Task, der bei jedem Versuch abstürzt (und
# dessen park()-Aufruf selbst aus irgendeinem Grund nicht durchkommt), die
# Spitze der Queue für immer, auch wenn der Daemon selbst längst bremst.
#
# WICHTIG: `attempts` zählt seit Plan 2 KONSEKUTIVE FEHLSCHLÄGE, nicht mehr
# Claims. Plan 1 kannte nur einen Tick pro Task (ein Tick = die ganze
# Aufgabe) — dort war "viermal geclaimt, nie fertig" gleichbedeutend mit
# "vergiftet". Plan 2s Pipeline (forge/pipeline.py) bringt einen Task pro
# Tick genau eine Stufe weiter; ein gesunder Task braucht mindestens fünf
# Ticks (spec, plan, implement, review, gate) und würde die alte, an
# claim_next() hängende Zählung allein durch normalen Fortschritt reißen —
# belegt durch den Akzeptanzlauf vom 2026-08-15 (Task 5: drei erfolgreiche
# Stufen, dann automatisch geparkt mit "4 Versuche ohne Erfolg"). Siehe
# `zaehle_fehlschlag` und `versuche_zuruecksetzen`.
AUTO_PARK_AFTER_ATTEMPTS = 3

# Quelle, deren Zeilen der Daemon nie anfasst. Zwei Testdateien
# (tests/test_forge_lebenszyklus.py, tests/test_forge_nachtlauf.py) legen echte
# Zeilen in forge_tasks an, und das Gate der Forge führt diese Tests im
# Worktree eines Tasks aus. Ohne diese Trennung könnte ein per SIGKILL
# beendeter Testlauf eine claimbare Zeile hinterlassen, die der nächste Tick
# mit echten LLM-Läufen bearbeitet — und umgekehrt könnte der Daemon einen
# Testtask claimen, während der Test noch läuft.
TEST_QUELLE = "test"


def _quellen_filter(quelle: str | None) -> tuple[str, tuple]:
    """SQL-Fragment und Parameter: Produktion (None) sieht alles ausser
    TEST_QUELLE, ein Test sieht ausschliesslich seine Quelle."""
    if quelle is None:
        return "AND source <> %s", (TEST_QUELLE,)
    return "AND source = %s", (quelle,)


def enqueue(title: str, description: str = "", source: str = "timo", priority: int = 50) -> int:
    """Reiht einen neuen Task ein und gibt seine ID zurück."""
    return db.insert_returning(
        "INSERT INTO forge_tasks (title, description, source, priority) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (title, description, source, priority),
    )


def active(quelle: str | None = None) -> dict | None:
    """Der Task, der gerade in Arbeit ist — oder None.

    Pausierte Tasks (Rate-Limit, Not-Aus) gelten NICHT als aktiv, sonst würde
    eine Pause den Daemon für ihre gesamte Dauer blockieren. `quelle` siehe
    TEST_QUELLE.
    """
    filter_sql, filter_params = _quellen_filter(quelle)
    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = ANY(%s) AND (paused_until IS NULL OR paused_until <= NOW()) "
        f"{filter_sql} ORDER BY updated_at ASC LIMIT 1",
        (list(m.ACTIVE_STATES), *filter_params),
    )
    return rows[0] if rows else None


def claim_next(quelle: str | None = None) -> dict | None:
    """Der Task, an dem als Nächstes gearbeitet wird.

    Zuerst ein bereits laufender (Wiederaufsetzen nach Absturz), sonst der
    oberste aus der Queue — der wird dabei auf die erste Stufe gesetzt.

    Rührt den Fehlschlag-Zähler (`attempts`) NICHT an: ein Claim allein ist
    kein Fehlschlag, sonst würde eine gesunde, mehrstufige Pipeline sich
    selbst Richtung Auto-Park zählen, nur weil sie mehrfach geclaimt wird
    (siehe AUTO_PARK_AFTER_ATTEMPTS). Wer einen Fehlschlag zählen will, ruft
    `zaehle_fehlschlag` auf; ein erfolgreicher Stufen-Abschluss ruft
    `versuche_zuruecksetzen`. `quelle` siehe TEST_QUELLE.
    """
    laufend = active(quelle)
    if laufend is not None:
        return laufend

    filter_sql, filter_params = _quellen_filter(quelle)
    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = %s AND (paused_until IS NULL OR paused_until <= NOW()) "
        f"{filter_sql} ORDER BY priority DESC, id ASC LIMIT 1",
        (m.QUEUED, *filter_params),
    )
    if not rows:
        return None

    task = rows[0]
    if not set_state(task["id"], m.SPECCING, current=m.QUEUED):
        return None
    task["state"] = m.SPECCING
    return task


def zaehle_fehlschlag(task_id: int, current: str) -> bool:
    """Zählt einen gescheiterten Bearbeitungsversuch am Task. Übersteigt der
    Zähler die Schwelle, wird der Task automatisch geparkt (Rückgabe True) —
    sonst nur hochgezählt (Rückgabe False).

    Ersetzt den früheren, an claim_next() hängenden Zähler: jetzt zählt nur
    ein tatsächlicher Fehlschlag, nicht jede Beanspruchung. In der Praxis
    parkt fast jeder Fehlschlag den Task ohnehin schon direkt mit einem
    spezifischeren Grund (siehe forge/pipeline.py und forge/daemon.py,
    Funktion `_park`); diese Funktion ist das Sicherheitsnetz für den Fall,
    dass genau dieser gezielte park()-Aufruf selbst wiederholt scheitert
    (CAS verloren) — ohne sie könnte ein solcher Task die Spitze der Queue
    für immer blockieren.
    """
    zeile = db.query_one(
        "UPDATE forge_tasks SET attempts=attempts+1, updated_at=NOW() WHERE id=%s RETURNING attempts",
        (task_id,),
    )
    versuche = int(zeile["attempts"]) if zeile else 0
    if versuche > AUTO_PARK_AFTER_ATTEMPTS:
        park(task_id, current=current,
             reason=f"Automatisch geparkt: {versuche} Fehlschläge in Folge")
        return True
    return False


def versuche_zuruecksetzen(task_id: int) -> None:
    """Setzt den Fehlschlag-Zähler zurück, nachdem eine Stufe sauber
    abgeschlossen hat. Das macht `attempts` zu einem 'hängt fest'-Detektor
    statt einem 'dauert lange'-Detektor: ein einzelner alter Fehlschlag darf
    einen inzwischen gesunden Task nicht weiter Richtung Park-Schwelle
    mitzählen, nur weil zwischendurch nie wieder auf 0 zurückgesetzt wurde."""
    db.execute("UPDATE forge_tasks SET attempts=0, updated_at=NOW() WHERE id=%s", (task_id,))


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


def merke_implement_modell(task_id: int, model: str) -> None:
    """Womit implementiert wurde — die Review-Stufe darf nicht dasselbe nehmen."""
    db.execute("UPDATE forge_tasks SET implement_model=%s, updated_at=NOW() WHERE id=%s", (model, task_id))


def zaehle_fixrunde(task_id: int) -> int:
    """Erhöht den Fix-Runden-Zähler und gibt den neuen Stand zurück."""
    zeile = db.query_one(
        "UPDATE forge_tasks SET refusals=refusals+1, updated_at=NOW() "
        "WHERE id=%s RETURNING refusals",
        (task_id,),
    )
    return int(zeile["refusals"]) if zeile else 0
