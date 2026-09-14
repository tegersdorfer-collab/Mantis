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
    # Nachtrag 2c: ein Fix bleibt in REVIEWING (kein Zustandswechsel, altes
    # Urteil wird verworfen, nächster Tick reviewt). Der frühere Übergang
    # REVIEWING -> IMPLEMENTING liess die Implement-Stufe nach jedem Fix ein
    # zweites Mal laufen und ist ersatzlos entfernt.
    REVIEWING: {GATING} | _ESCAPES,
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
