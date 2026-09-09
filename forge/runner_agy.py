"""Führt die Review-Stufe über die `agy`-CLI (Antigravity) aus.

Bewusst OHNE Werkzeuge: agys Rechtemodell ist nicht gemessen (anders als
opencodes, siehe tests/fixtures/permission_probe_opencode.md). Solange das so
ist, bekommt der Reviewer keinen Dateizugriff.

Zwei Folgen daraus, die dieses Modul trägt:
1. Der Diff wird aus .forge/diff.patch gelesen und in den Prompt eingebettet,
   statt ihn den Agenten lesen zu lassen.
2. Das Urteil kommt auf stdout, und dieses Modul schreibt .forge/review.json —
   die Artefaktprüfung in forge/stages.py bleibt dadurch unverändert gültig.
"""
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from forge.runner import RunResult
from forge.stages import DIFF_DATEI, VERDIKT_DATEI

log = logging.getLogger(__name__)

AGY_BIN = shutil.which("agy")

# JSON entweder blank oder in einem ```json-Block — Modelle liefern beides.
_CODEBLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _finde_json(text: str) -> dict | None:
    for kandidat in (text.strip(), *(m.group(1) for m in _CODEBLOCK.finditer(text))):
        try:
            objekt = json.loads(kandidat)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(objekt, dict):
            return objekt
    return None


def run(prompt: str, cwd: Path, timeout: int, agent: str, model: str) -> RunResult:
    """Führt einen werkzeuglosen agy-Lauf aus und schreibt das Verdikt.

    `agent` wird hier nicht benutzt und ist trotzdem Teil der Signatur: alle
    Backends werden über forge.backends.hole() einheitlich aufgerufen, und die
    Pipeline reicht den Stufennamen an jedes durch. Eine abweichende Signatur
    würde die Registry zu einer Sonderfallbehandlung zwingen.
    """
    del agent  # bewusst ungenutzt, siehe Docstring
    if AGY_BIN is None:
        return RunResult(ok=False, error="agy-Binary nicht im PATH gefunden")

    diff_pfad = Path(cwd) / DIFF_DATEI
    if not diff_pfad.exists():
        # Ohne Diff würde der Reviewer über nichts urteilen. Das ist ein
        # Pipeline-Fehler, kein leeres Review.
        return RunResult(ok=False, error=f"{DIFF_DATEI} fehlt — Pipeline hat sie nicht geschrieben")

    voll = (
        f"{prompt}\n\n"
        "Der zu beurteilende Diff:\n"
        "```diff\n" + diff_pfad.read_text(encoding="utf-8", errors="replace") + "\n```\n\n"
        'Antworte AUSSCHLIESSLICH mit JSON der Form '
        '{"ok": true|false, "befunde": ["..."]}. Kein weiterer Text.'
    )

    try:
        ergebnis = subprocess.run(
            [AGY_BIN, "-p", voll, "--model", model],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, error=f"Zeitüberschreitung nach {timeout}s")
    except OSError as exc:
        return RunResult(ok=False, error=f"Aufruf fehlgeschlagen: {exc}")

    ausgabe = ergebnis.stdout or ""
    verdikt = _finde_json(ausgabe)
    if verdikt is None:
        return RunResult(ok=False, text=ausgabe,
                         error="Reviewer lieferte kein JSON-Verdikt")

    ziel = Path(cwd) / VERDIKT_DATEI
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(verdikt, ensure_ascii=False), encoding="utf-8")
    return RunResult(ok=True, text=ausgabe)
