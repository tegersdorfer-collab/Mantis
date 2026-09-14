"""Führt die Review-Stufe über die `agy`-CLI (Antigravity) aus.

Der Reviewer BRAUCHT keine Werkzeuge: der Diff wird ihm in den Prompt gelegt,
und sein Urteil kommt auf stdout zurück. Erzwungen ist die Werkzeuglosigkeit
aber NICHT — `agy` ist eine vollwertige agentische CLI und hat (Stand
1.x, `agy --help`) keine Option, die das Werkzeugangebot abschaltet.
Vorhanden sind nur `--sandbox` (Terminal-Beschränkung) und
`--dangerously-skip-permissions` (das Gegenteil). Die frühere Behauptung
"bewusst OHNE Werkzeuge" war damit eine Zusicherung ohne Mechanismus.

Was dieses Modul deshalb tatsächlich leistet:
1. Der Diff wird aus .forge/diff.patch gelesen und in den Prompt eingebettet,
   statt sich darauf zu verlassen, dass der Agent ihn selbst lesen kann.
2. Das Urteil kommt auf stdout, und dieses Modul schreibt .forge/review.json —
   die Artefaktprüfung in forge/stages.py bleibt dadurch unverändert gültig.
3. Der Lauf bekommt `cwd=<worktree>`. `agy` kennt kein `--dir`; sein
   Arbeitsverzeichnis ist das des Elternprozesses — ohne cwd wäre das der
   Daemon-Prozess und damit der echte Haupt-Checkout, nicht der Worktree.

Das geschriebene Verdikt hält sich exakt an das Schema, das forge/gate.py
liest: {"verdict": "pass"|"fail", "findings": [...]}. Ein Urteil in einer
anderen Form ist ein Runner-Fehler und wird NICHT geschrieben — ein still
geschriebenes Verdikt, das das Gate nicht versteht, ist ein rotes Gate für
jeden Task (Kritischer Fund C1, Abschluss-Review 2026-09-09).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from forge import runner
from forge.runner import RunResult
from forge.stages import DIFF_DATEI, VERDIKT_DATEI

AGY_BIN = shutil.which("agy")

# JSON entweder blank oder in einem ```json-Block — Modelle liefern beides.
_CODEBLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

# Die beiden Werte, die forge/gate.py auseinanderhalten kann. Alles andere
# wäre für das Gate stillschweigend "nicht pass" — und ein Urteil, das der
# Reviewer so nicht gefällt hat, darf nicht durch eine Schema-Panne entstehen.
_ERLAUBTE_URTEILE = frozenset({"pass", "fail"})

# Das Schema, das der Reviewer liefern muss. Wörtlich dieselben Felder und
# Schweregrade, die forge/gate.py auswertet (_HARTE_BEFUNDE dort).
_SCHEMA_ANWEISUNG = (
    'Antworte AUSSCHLIESSLICH mit JSON dieser Form, ohne weiteren Text:\n'
    '{"verdict": "pass" oder "fail", "findings": [{"severity": '
    '"critical|important|minor", "file": "<pfad>", "what": "<befund>"}]}\n'
    "verdict ist 'fail', sobald mindestens ein Befund critical oder important "
    "ist. Ohne Befunde ist findings eine leere Liste."
)


def _finde_json(text: str) -> dict | None:
    for kandidat in (text.strip(), *(m.group(1) for m in _CODEBLOCK.finditer(text))):
        try:
            objekt = json.loads(kandidat)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(objekt, dict):
            return objekt
    return None


def _normalisiere_verdikt(roh: dict) -> tuple[dict | None, str | None]:
    """Prüft das Modell-JSON gegen das Schema von forge/gate.py.

    Gibt (verdikt, None) zurück, wenn es passt, sonst (None, Fehlertext). Es
    wird bewusst ein neues, auf genau zwei Schlüssel reduziertes Objekt
    gebaut: was der Reviewer sonst noch mitschickt, hat im Artefakt nichts
    verloren und könnte das Gate nur verwirren.
    """
    urteil = roh.get("verdict")
    if not isinstance(urteil, str):
        art = type(urteil).__name__
        return None, (f"Verdikt ohne verwertbares Feld 'verdict' ({art}) — "
                      f"forge/gate.py könnte es nicht lesen")
    normiert = urteil.strip().lower()
    if normiert not in _ERLAUBTE_URTEILE:
        return None, (f"Feld 'verdict' hat den unbekannten Wert {urteil!r} — "
                      f"erlaubt sind {sorted(_ERLAUBTE_URTEILE)}")
    befunde = roh.get("findings", [])
    if befunde is None:
        befunde = []
    if not isinstance(befunde, list):
        art = type(befunde).__name__
        return None, f"Feld 'findings' ist {art} statt Liste"
    return {"verdict": normiert, "findings": befunde}, None


def run(prompt: str, cwd: Path, timeout: int, agent: str, model: str) -> RunResult:
    """Führt einen agy-Lauf aus und schreibt das Verdikt.

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
        + _SCHEMA_ANWEISUNG
    )

    try:
        ergebnis = subprocess.run(
            # agys eigener --print-timeout (Default 5m0s) läge sonst unter dem
            # Stufen-Timeout: ein langes Opus-Thinking-Review bräche agy selbst
            # ab, und die Pipeline sähe einen leeren Lauf ohne Verdikt
            # (Befund vor der ersten Nacht, 2026-09-14). Go-Dauerformat.
            [AGY_BIN, "-p", voll, "--model", model, "--print-timeout", f"{timeout}s"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd), stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, error=f"Zeitüberschreitung nach {timeout}s")
    except OSError as exc:
        return RunResult(ok=False, error=f"Aufruf fehlgeschlagen: {exc}")

    ausgabe = ergebnis.stdout or ""
    # Kontingentgrenzen sind bei Gratis-Anbietern der Normalfall, nicht die
    # Ausnahme. Ohne dieses Flag parkt forge/pipeline.py den Task, statt den
    # Zustand stehen zu lassen und es später erneut zu versuchen.
    limitiert = runner._ist_rate_limit(ausgabe + "\n" + (ergebnis.stderr or ""))

    verdikt = _finde_json(ausgabe)
    if verdikt is None:
        return RunResult(ok=False, text=ausgabe, rate_limited=limitiert,
                         error="Reviewer lieferte kein JSON-Verdikt")

    geprueft, fehler = _normalisiere_verdikt(verdikt)
    if geprueft is None:
        return RunResult(ok=False, text=ausgabe, rate_limited=limitiert, error=fehler)

    ziel = Path(cwd) / VERDIKT_DATEI
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(geprueft, ensure_ascii=False), encoding="utf-8")
    return RunResult(ok=True, text=ausgabe)
