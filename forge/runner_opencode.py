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
import json
import logging
from typing import Iterable

from forge.backends import OpencodePermission
from forge.runner import RunResult

log = logging.getLogger(__name__)

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


# Marker im Fehlertext, an dem ein von opencode entferntes Werkzeug erkannt
# wird. Aufgenommen in tests/fixtures/opencode_stream_denied.jsonl: opencode
# nimmt ein verbotenes Werkzeug aus der Werkzeugliste, das Modell ruft es
# trotzdem, und der Anbieter lehnt mit dieser Meldung ab.
_DENIAL_MARKER = "was not in request.tools"


def parse_events(lines: Iterable[str]) -> RunResult:
    """Wertet den `--format json`-Strom von opencode aus.

    Robust gegen Nicht-JSON-Zeilen und gegen gültiges JSON, das kein Objekt
    ist — dieselbe Begründung wie in forge/runner.py:parse_stream.
    """
    ereignisse: list[dict] = []
    for zeile in lines:
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            objekt = json.loads(zeile)
        except json.JSONDecodeError:
            continue
        if isinstance(objekt, dict):
            ereignisse.append(objekt)

    if not ereignisse:
        return RunResult(ok=False, error="opencode lieferte keine Ereignisse")

    texte: list[str] = []
    denials: list[dict] = []
    fehler: str | None = None
    tin = tout = cread = ccreate = 0

    for e in ereignisse:
        art = e.get("type")
        teil = e.get("part") or {}
        if art == "text":
            texte.append(teil.get("text", ""))
        elif art == "step_finish":
            tok = teil.get("tokens") or {}
            tin += int(tok.get("input") or 0)
            tout += int(tok.get("output") or 0)
            cache = tok.get("cache") or {}
            cread += int(cache.get("read") or 0)
            ccreate += int(cache.get("write") or 0)
        elif art == "error":
            meldung = json.dumps(e.get("error") or {})
            if _DENIAL_MARKER in meldung:
                denials.append({"message": meldung})
            fehler = meldung

    return RunResult(
        ok=fehler is None,
        text="".join(texte).strip(),
        tokens_in=tin,
        tokens_out=tout,
        error=fehler,
        denials=denials,
        cache_read=cread,
        cache_creation=ccreate,
        raw=ereignisse,
    )
