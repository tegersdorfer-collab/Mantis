"""Führt eine Forge-Stufe über die `opencode`-CLI aus.

Ein Lauf pro Stufe, frischer Kontext, Übergabe über Dateien im Worktree —
dieselbe Bauart wie forge/runner.py, nur mit anderer CLI.

Die Rechte kommen ausschliesslich aus forge/backends.OpencodePermission und
werden je Lauf in eine temporäre Config geschrieben, die über die
Umgebungsvariable OPENCODE_CONFIG gesetzt wird. Die globale Config des
Benutzers (~/.config/opencode/opencode.json) wird damit NICHT verwendet — ein
unbeaufsichtigter Lauf darf nicht davon abhängen, was Timo dort gerade
eingestellt hat.
"""
from forge.backends import OpencodePermission

# Welche Provider in der Lauf-Config stehen. Die Schlüssel selbst stehen nie
# in der Datei, nur der Verweis auf die Umgebungsvariable — die Config landet
# in /tmp und soll dort keine Geheimnisse hinterlassen.
_PROVIDER_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

# Sitzungstitel und andere Kleinstaufgaben. Groq hat 8000 Tokens/Minute und
# taugt damit nicht für Arbeitsstufen, für Titel aber sehr wohl.
SMALL_MODEL = "groq/openai/gpt-oss-120b"


def baue_config(agent: str, model: str) -> dict:
    """Die opencode-Config für genau einen Lauf."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": SMALL_MODEL,
        "provider": {
            name: {"options": {"apiKey": "{env:" + env + "}"}}
            for name, env in _PROVIDER_ENV.items()
        },
        "agent": {
            agent: {
                "mode": "primary",
                "model": model,
                "permission": OpencodePermission().als_dict(),
            }
        },
    }
