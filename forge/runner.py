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
