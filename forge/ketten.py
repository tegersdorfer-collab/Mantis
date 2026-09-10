"""Welches Modell eine Stufe benutzt, wenn das bevorzugte nicht mehr kann.

Eine Kette ist geordnet: das erste Glied ist die erste Wahl. Ist sein Anbieter
erschöpft, rückt das nächste nach. Ist die Kette leer, liefert `waehle` None —
der Aufrufer parkt den Task dann, statt ihn scheitern zu lassen.

Alle Modelle sind am 2026-09-07 gegen die echten Anbieter geprüft worden;
DeepSeek V4 (Zeitüberschreitung), kimi-k2.6 und nemotron-ultra-253b (404 für
diesen Account) und gemma-4-31b (Zeitüberschreitung) stehen deshalb NICHT
hier drin, obwohl Kataloge sie führen.
"""
from forge import budget

# Stufenname → geordnete Folge von (backend, model).
KETTEN: dict[str, tuple[tuple[str, str], ...]] = {
    "spec": (
        ("opencode", "google/gemini-3.6-flash"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "nvidia/minimaxai/minimax-m3"),
    ),
    "plan": (
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
        ("opencode", "nvidia/minimaxai/minimax-m3"),
    ),
    # Die letzten Glieder von implement und fix liegen bewusst bei einem
    # ANDEREN Anbieter. Erschöpfung gilt anbieterweit — eine Kette aus lauter
    # NVIDIA-Modellen wäre mit einer einzigen Rate-Limit-Meldung komplett tot,
    # und zwar bei der Stufe, ohne die kein Task fertig wird. Gemini Flash ist
    # für Code schwächer als MiniMax; ein schwächeres Modell, dessen Arbeit das
    # Gate prüft und Timo freigibt, ist besser als gar keine Stufe.
    "implement": (
        ("opencode", "nvidia/minimaxai/minimax-m3"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "fix": (
        ("opencode", "nvidia/minimaxai/minimax-m3"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "review": (
        ("agy", "claude-opus-4-6-thinking"),
        ("agy", "gemini-3.1-pro-high"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
    ),
}


def waehle(stufe_name: str, verboten: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """Das erste nutzbare Glied der Kette, oder None.

    `verboten` trägt die Reviewer-Regel: die Review-Stufe bekommt hier das
    Modell hinein, mit dem implementiert wurde. Bleibt danach nichts übrig,
    ist None die richtige Antwort — der Task wird geparkt statt von dem
    Modell abgenommen, das ihn geschrieben hat.
    """
    for backend, model in KETTEN.get(stufe_name, ()):
        if model in verboten:
            continue
        if budget.ist_erschoepft(model):
            continue
        return backend, model
    return None
