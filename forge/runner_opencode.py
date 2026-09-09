"""Führt eine Forge-Stufe über die `opencode`-CLI aus.

Ein Lauf pro Stufe, frischer Kontext, Übergabe über Dateien im Worktree —
dieselbe Bauart wie forge/runner.py, nur mit anderer CLI.

Die Rechte kommen aus forge/backends.OpencodePermission und werden je Lauf in
eine temporäre Config geschrieben, die über die Umgebungsvariable
OPENCODE_CONFIG gesetzt wird.

Wie opencode 1.18.20 Configs tatsächlich schichtet (C2, Abschluss-Review
2026-09-09 — die frühere Behauptung "die globale Config wird damit NICHT
verwendet" war in beiden Hälften falsch):

    globale Config (~/.config/opencode/opencode.json)
      → OPENCODE_CONFIG (unsere Lauf-Config)
      → Projekt-Configs, von --dir aufwärts gesucht  ← gewinnen
      → OPENCODE_CONFIG_CONTENT

Zwei Folgen:

1. Die globale Config des Benutzers wird sehr wohl gemergt. Unsere Lauf-Config
   überschreibt sie in allem, was sie selbst setzt (Modell, Agent, Rechte),
   aber die Modell-Deklarationen (limit, tool_call) der NVIDIA-Modelle stehen
   heute NUR dort. Ein Lauf hängt insoweit weiterhin an Timos globaler Config.
2. Projekt-Configs schlagen unsere. Eine implement-Stufe hat `edit: allow` in
   ihrem Worktree und könnte dort ein `opencode.json` mit
   {"agent":{"implement":{"permission":{"bash":"allow"}}}} anlegen — der
   nächste Lauf im selben Worktree hätte damit eine Shell, und Probe (d) in
   tests/fixtures/permission_probe_opencode.md zeigt, dass eine Shell der
   vollständige Ausbruch bis ~/.config/ai-keys.env ist. Der confined Agent
   könnte also seine eigene Einhegung aufheben. Dagegen setzt run()
   OPENCODE_DISABLE_PROJECT_CONFIG=1 in die Laufumgebung.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from forge import runner
from forge.backends import OpencodePermission
from forge.runner import RunResult

log = logging.getLogger(__name__)

# Einmal beim Import aufgelöst, nicht je Lauf — dieselbe Begründung wie
# CLAUDE_BIN in forge/runner.py: ein launchd-Agent hat einen minimalen PATH,
# und ein fehlendes Binary soll als klare Meldung auftauchen, nicht als
# generischer OSError aus subprocess.
OPENCODE_BIN = shutil.which("opencode")

# Schaltet die Projekt-Config-Suche ab, die sonst unsere Lauf-Config schlagen
# würde (siehe Modul-Docstring). Im Binary von opencode 1.18.20 nachgewiesen —
# keine erfundene Variable.
PROJEKT_CONFIG_AUS = "OPENCODE_DISABLE_PROJECT_CONFIG"

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

# Der Werkzeugname aus genau dieser Meldung. forge/pipeline.py liest ihn als
# `tool_name`; ohne ihn nannte der Park-Grund das verweigerte Werkzeug als "?"
# und war damit für eine nächtliche Nachschau wertlos (I5).
_DENIAL_WERKZEUG = re.compile(r"tool '([^']+)' which was not in request\.tools")

# Der `reason` des letzten step_finish eines regulär beendeten Laufs. Belegt in
# tests/fixtures/opencode_stream_success.jsonl. Ohne diese Prüfung meldete ein
# abgeschnittener Strom (OOM-Kill, Signal, Output-Limit) ok=True mit halb
# geschriebenem Code — parse_events verlangte bisher kein Abschlussereignis,
# anders als forge/runner.py:parse_stream, das ohne Result-Event ablehnt (I3).
#
# Die Prüfung ist bewusst strikt (nur "stop" gilt), nach derselben Regel wie
# das fehlende is_error in forge/runner.py:parse_stream: für einen
# unbeaufsichtigten, geldkostenden Prozess muss UNBEKANNT als Fehlschlag
# gelten. Belegt ist bislang nur diese eine Aufnahme; taucht im Betrieb ein
# weiterer regulärer Abschlussgrund auf, gehört er hierher — mit Aufnahme,
# nicht auf Verdacht.
_ABSCHLUSS_GRUND = "stop"


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
    abschluss_gruende: list[str | None] = []
    fehler: str | None = None
    tin = tout = cread = ccreate = 0

    for e in ereignisse:
        art = e.get("type")
        teil = e.get("part") or {}
        if art == "text":
            texte.append(teil.get("text", ""))
        elif art == "step_finish":
            grund = teil.get("reason")
            abschluss_gruende.append(grund if isinstance(grund, str) else None)
            tok = teil.get("tokens") or {}
            tin += int(tok.get("input") or 0)
            tout += int(tok.get("output") or 0)
            cache = tok.get("cache") or {}
            cread += int(cache.get("read") or 0)
            ccreate += int(cache.get("write") or 0)
        elif art == "error":
            meldung = json.dumps(e.get("error") or {})
            if _DENIAL_MARKER in meldung:
                eintrag: dict = {"message": meldung}
                treffer = _DENIAL_WERKZEUG.search(meldung)
                if treffer:
                    eintrag["tool_name"] = treffer.group(1)
                denials.append(eintrag)
            fehler = meldung

    if fehler is None and (not abschluss_gruende or abschluss_gruende[-1] != _ABSCHLUSS_GRUND):
        # Kein Abschlussereignis (Prozess gestorben) oder ein anderer Grund als
        # "stop" (z.B. "length" = Ausgabe abgeschnitten, "tool-calls" = der Lauf
        # war mitten in einer Werkzeugrunde). In allen Fällen liegt halbe Arbeit
        # vor, und die darf nicht als Erfolg durchgehen.
        letzter = abschluss_gruende[-1] if abschluss_gruende else "(kein step_finish)"
        fehler = (f"opencode-Strom endet nicht regulär (letzter step_finish: {letzter!r}) — "
                  f"abgebrochener oder abgeschnittener Lauf")
        log.warning(f"Forge-Runner: {fehler}")

    text = "".join(texte).strip()
    return RunResult(
        ok=fehler is None,
        text=text,
        tokens_in=tin,
        tokens_out=tout,
        error=fehler,
        # Kontingentgrenzen sind bei Gratis-Anbietern der Normalfall. Ohne
        # dieses Flag parkt forge/pipeline.py den Task, statt den Zustand
        # stehen zu lassen und es später erneut zu versuchen (I2).
        rate_limited=fehler is not None and runner._ist_rate_limit(f"{fehler}\n{text}"),
        denials=denials,
        cache_read=cread,
        cache_creation=ccreate,
        raw=ereignisse,
    )


def run(prompt: str, cwd: Path, timeout: int, agent: str, model: str) -> RunResult:
    """Führt eine Stufe über opencode im angegebenen Worktree aus."""
    if OPENCODE_BIN is None:
        fehler = f"opencode-Binary nicht im PATH gefunden (PATH={os.environ.get('PATH', '')})"
        log.warning(f"Forge-Runner: {fehler}")
        return RunResult(ok=False, error=fehler)

    config = baue_config(agent, model)
    datei = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="forge-oc-", delete=False, encoding="utf-8"
    )
    try:
        json.dump(config, datei)
        datei.close()

        umgebung = dict(os.environ)
        umgebung["OPENCODE_CONFIG"] = datei.name
        # C2: ohne das schlägt ein <worktree>/opencode.json unsere Lauf-Config
        # (siehe Modul-Docstring) — eine Stufe mit `edit: allow` könnte sich
        # damit selbst `bash: allow` erteilen und wäre nicht mehr eingehegt.
        umgebung[PROJEKT_CONFIG_AUS] = "1"

        try:
            ergebnis = subprocess.run(
                [OPENCODE_BIN, "run", "--agent", agent, "--dir", str(cwd),
                 "--format", "json", prompt],
                capture_output=True, text=True, timeout=timeout,
                env=umgebung, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, error=f"Zeitüberschreitung nach {timeout}s")
        except OSError as exc:
            return RunResult(ok=False, error=f"Aufruf fehlgeschlagen: {exc}")

        resultat = parse_events((ergebnis.stdout or "").splitlines())
        # I3: returncode und stderr wurden bisher verworfen. Ein Strom kann
        # vollständig aussehen und der Prozess trotzdem unsauber gestorben
        # sein — und die Kontingentmeldung eines Anbieters steht oft nur auf
        # stderr, nie im Ereignisstrom.
        if ergebnis.returncode != 0:
            fehlerstrom = (ergebnis.stderr or "").strip()
            if resultat.ok:
                resultat.ok = False
                resultat.error = (f"opencode endete mit returncode {ergebnis.returncode}: "
                                  f"{fehlerstrom[:300] or 'ohne stderr'}")
            elif fehlerstrom:
                resultat.error = (f"{resultat.error} (returncode {ergebnis.returncode}: "
                                  f"{fehlerstrom[:300]})")
            resultat.rate_limited = resultat.rate_limited or runner._ist_rate_limit(fehlerstrom)
        return resultat
    finally:
        # Die Config enthält keine Schlüssel, nur Verweise — trotzdem nicht
        # in /tmp liegen lassen.
        try:
            os.unlink(datei.name)
        except OSError:
            pass
