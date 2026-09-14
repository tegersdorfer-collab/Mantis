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
# Nachtrag 2c: das grüne Gate wartet hier auf Timos Freigabe (forge/cli.py,
# ab Plan 3 der Telegram-Bot). Gehört bewusst NICHT zu ACTIVE_STATES — ein
# Task, der auf Timo wartet, blockiert keine Bahn. Der frühere Zustand
# awaiting_restart_window (Daemon-Neustart nach Merges in forge/) ist
# entfernt: die Agenten dürfen forge/ nicht anfassen, kein Merge braucht
# einen Neustart, und am 2026-09-14 stand keine Zeile in dem Zustand.
AWAITING_APPROVAL = "awaiting_approval"
MERGED = "merged"
PARKED = "parked"
FAILED = "failed"

# Zustände, in denen ein Task als "in Arbeit" gilt. Findet der Daemon beim Start
# einen solchen Task, setzt er dort wieder auf, statt einen neuen zu ziehen.
ACTIVE_STATES = frozenset({
    SPECCING, PLANNING, IMPLEMENTING, REVIEWING, GATING,
})

# Jede Pipeline-Stufe darf jederzeit parken oder endgültig scheitern.
_ESCAPES = {PARKED, FAILED}

_TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {SPECCING} | _ESCAPES,
    SPECCING: {PLANNING} | _ESCAPES,
    PLANNING: {IMPLEMENTING} | _ESCAPES,
    IMPLEMENTING: {REVIEWING} | _ESCAPES,
    # Nachtrag 2c: ein Fix bleibt in REVIEWING (kein Zustandswechsel, altes
    # Urteil wird verworfen, nächster Tick reviewt). Der frühere Übergang
    # REVIEWING -> IMPLEMENTING liess die Implement-Stufe nach jedem Fix ein
    # zweites Mal laufen und ist ersatzlos entfernt.
    REVIEWING: {GATING} | _ESCAPES,
    # Kein automatischer Merge aus GATING, auch nicht für Docs (Spec,
    # "Bewusst nicht enthalten"). Grün heisst: warten auf Timo.
    GATING: {AWAITING_APPROVAL} | _ESCAPES,
    AWAITING_APPROVAL: {MERGED, PARKED},
    MERGED: set(),                  # Endzustand
    PARKED: {QUEUED},               # Freigabe durch Timo
    FAILED: {QUEUED},               # manueller Neuanlauf
}


def can_transition(current: str, target: str) -> bool:
    """Ist der Übergang erlaubt? Unbekannte Zustände sind immer nein."""
    return target in _TRANSITIONS.get(current, set())
