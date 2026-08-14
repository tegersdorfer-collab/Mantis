"""Startet headless Claude-Code-Läufe und wertet ihre Ausgabe aus.

Ein Lauf pro Pipeline-Stufe, jeweils mit frischem Kontext. Die Übergabe
zwischen Stufen läuft über Dateien im Worktree, nicht über Gesprächsverlauf —
deshalb genügt hier ein einzelner Aufruf ohne Sitzungsverwaltung.
"""
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Textmarker, an denen ein erschöpftes Kontingent erkannt wird. Plan 3 nutzt das
# Flag, um bis zum Reset zu schlafen, statt den Task zu parken.
_RATE_LIMIT_MARKER = ("usage limit reached", "rate limit", "rate_limit")

# Modi, die für einen unbeaufsichtigten Daemon in Frage kommen. bypassPermissions
# ist bewusst NICHT dabei: ein Prozess, der nachts ohne Aufsicht läuft, darf sich
# nicht selbst alle Rechte erteilen — das ist der ganze Sinn der Profile.
_ERLAUBTE_MODI = frozenset({"acceptEdits", "dontAsk", "plan", "default", "auto"})

# Einmal beim Import aufgelöst, nicht bei jedem Lauf: ein launchd-User-Agent
# bekommt standardmäßig nur PATH=/usr/bin:/bin:/usr/sbin:/sbin — dort liegt
# `claude` nicht. Ohne diese Prüfung landet die Suche erst in subprocess.run(),
# wo ein fehlendes Binary nur als generischer OSError sichtbar wird, der sich
# nicht von "Berechtigung fehlt" oder "Datenträger voll" unterscheiden lässt.
CLAUDE_BIN = shutil.which("claude")


@dataclass
class RunResult:
    ok: bool
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    rate_limited: bool = False
    raw: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionProfile:
    """Womit ein einzelner Lauf arbeiten darf.

    `allowed` folgt der CLI-Syntax (verifiziert 2026-08-14): Tool-Namen oder
    Muster wie "Bash(git *)", von der CLI leerzeichengetrennt erwartet.

    Der Default-Modus ist ABSICHTLICH `dontAsk`, nicht `acceptEdits`: die
    Aufnahme in tests/fixtures/permission_probe.md (Probe b1) zeigt, dass
    `acceptEdits` einen Write-Aufruf durchwinkt, obwohl `Write` nicht in
    `allowed` stand — das Profil wäre für Datei-Edits wirkungslos. `dontAsk`
    verweigert denselben Zugriff nachweislich korrekt (sichtbar in
    `permission_denials`) und hängt dabei nicht.
    """
    allowed: tuple[str, ...]
    mode: str = "dontAsk"

    def __post_init__(self):
        if not self.allowed:
            raise ValueError("Rechteprofil ohne Tools — der Lauf könnte nur hängen bleiben")
        for tool in self.allowed:
            if not tool or not tool.strip():
                raise ValueError(f"Ungültiger Werkzeugname im Rechteprofil: {tool!r}")
        if self.mode not in _ERLAUBTE_MODI:
            raise ValueError(f"Unzulässiger Permission-Modus: {self.mode}")


def _ist_rate_limit(text: str) -> bool:
    klein = (text or "").lower()
    return any(marker in klein for marker in _RATE_LIMIT_MARKER)


def parse_stream(lines: Iterable[str]) -> RunResult:
    """Wertet die stream-json-Ausgabe aus.

    Robust gegen Nicht-JSON-Zeilen: die CLI mischt gelegentlich Klartext dazu,
    und daran darf ein sonst erfolgreicher Lauf nicht scheitern. Ebenso robust
    gegen gültiges JSON, das kein Objekt ist (z.B. `null` oder `3`) — nur
    JSON-Objekte kennen `.get()`.
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

    schluss = next((e for e in reversed(ereignisse) if e.get("type") == "result"), None)
    if schluss is None:
        fehler = "kein Ergebnis im Stream (Lauf abgebrochen?)"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(ok=False, error=fehler, raw=ereignisse)

    text = schluss.get("result") or ""
    verbrauch = schluss.get("usage") or {}
    tokens_in = int(verbrauch.get("input_tokens") or 0)
    tokens_out = int(verbrauch.get("output_tokens") or 0)

    ist_fehler = schluss.get("is_error")
    if ist_fehler is None:
        # Fehlt das Feld ganz, ist der Status UNBEKANNT — nicht "fine". Für einen
        # unbeaufsichtigten, geldkostenden Prozess muss Unbekannt als Fehlschlag
        # gelten, sonst geht ein stiller Fehlerfall als Erfolg durch.
        fehler = "is_error fehlt im Ergebnis-Event — Status unbekannt, gilt als Fehlschlag"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(
            ok=False, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            error=fehler, rate_limited=_ist_rate_limit(text), raw=ereignisse,
        )

    if ist_fehler:
        fehler = text or schluss.get("subtype") or "unbekannter Fehler"
        log.warning(f"Forge-Runner: Lauf fehlgeschlagen: {fehler}")
        return RunResult(
            ok=False, text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            error=fehler, rate_limited=_ist_rate_limit(text), raw=ereignisse,
        )

    return RunResult(ok=True, text=text, tokens_in=tokens_in, tokens_out=tokens_out, raw=ereignisse)


def run(prompt: str, cwd: Path, timeout: int = 1800, profile: PermissionProfile | None = None) -> RunResult:
    """Führt einen headless Lauf im angegebenen Worktree aus.

    Ohne `profile` erbt der Lauf die Rechte aus ~/.claude/settings.json des
    aufrufenden Users — das ist das Verhalten aus Plan 1 und bleibt für
    bestehende Aufrufer unverändert. Mit `profile` wird der Lauf explizit auf
    die dort genannten Werkzeuge und den Permission-Modus eingeschränkt.
    """
    if CLAUDE_BIN is None:
        fehler = f"claude-Binary nicht im PATH gefunden (PATH={os.environ.get('PATH', '')})"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(ok=False, error=fehler)

    befehl = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if profile is not None:
        befehl += ["--allowedTools", " ".join(profile.allowed), "--permission-mode", profile.mode]
    try:
        fertig = subprocess.run(
            # stdin MUSS abgeklemmt werden: ohne DEVNULL wartet die CLI drei Sekunden
            # auf Eingabe und schreibt eine Warnung — pro Stufe, bei jedem Lauf.
            # Live gemessen am 2026-08-13.
            befehl, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        fehler = f"Zeitüberschreitung nach {timeout}s"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(ok=False, error=fehler)
    except OSError as exc:
        # Zweite Verteidigungslinie für den Fall, dass das Binary zwischen der
        # Auflösung von CLAUDE_BIN und diesem Aufruf verschwindet (Race, kaputte
        # Rechte) — der Normalfall ist oben bereits abgefangen.
        fehler = f"claude nicht startbar: {exc}"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(ok=False, error=fehler)

    ergebnis = parse_stream(fertig.stdout.splitlines())
    if not ergebnis.ok and fertig.stderr.strip():
        ergebnis.error = f"{ergebnis.error} | stderr: {fertig.stderr.strip()[:500]}"
        if _ist_rate_limit(fertig.stderr):
            ergebnis.rate_limited = True
    return ergebnis
