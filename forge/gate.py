"""Das Gate — die letzte Instanz vor dem Merge.

Bewusst ohne LLM. Alles andere in der Pipeline ist ein Sprachmodell, das sich
selbst beurteilt; hier entscheiden Rückgabewerte. Ein Lauf kann das Gate nicht
überreden, nur bestehen.

Der Merge selbst passiert hier NICHT — das ist Plan 3. Das Gate liefert nur
das Urteil.
"""
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from forge import gitctl

log = logging.getLogger(__name__)

# Pfad-Präfixe, die kein automatischer Merge anfassen darf.
SPERRZONEN = (
    ".env",
    "data/",
    "forge/gate.py",
    "forge/budget.py",
    "Library/LaunchAgents/",
    "scripts/fix_bluetooth.sh",
)

# Darüber ist es kein Task mehr, sondern ein Projekt — dann soll ein Mensch schauen.
MAX_DIFF_ZEILEN = 800

VERDIKT_DATEI = ".forge/review.json"

# Befunde ab dieser Schwere kippen ein 'pass' des Review-Agenten.
_HARTE_BEFUNDE = frozenset({"critical", "important"})

# Zeitbudget für die beiden externen Aufrufe, die pruefe() macht.
_TEST_TIMEOUT_SEKUNDEN = 1800
_LINT_TIMEOUT_SEKUNDEN = 300


@dataclass
class GateErgebnis:
    ok: bool
    gruende: list[str] = field(default_factory=list)


def _normalisiere_pfad(pfad: str) -> str:
    """Entfernt ein führendes "./" oder "/", ohne str.lstrip()'s Zeichenklassen-
    Falle: lstrip("./") würde ".env" fälschlich zu "env" verstümmeln, weil es
    jedes führende Zeichen aus der Menge {'.', '/'} einzeln abschneidet statt
    das Präfix "./" als Ganzes zu behandeln."""
    while pfad.startswith("./"):
        pfad = pfad[2:]
    return pfad.lstrip("/")


def beruehrt_sperrzone(dateien: list[str]) -> list[str]:
    """Welche der Dateien liegen in einer Sperrzone?"""
    treffer = []
    for datei in dateien:
        pfad = _normalisiere_pfad(datei)
        for zone in SPERRZONEN:
            # Verzeichniszonen enden auf "/" und matchen als Präfix; Dateizonen
            # müssen exakt passen, sonst würde "datastore.py" an "data/" hängen.
            if (zone.endswith("/") and pfad.startswith(zone)) or pfad == zone:
                treffer.append(datei)
                break
    return treffer


def lies_verdikt(worktree: Path) -> tuple[bool, list[dict]]:
    """Liest das Review-Urteil. Fehlt es oder ist es kaputt, gilt das als 'fail'.

    Ein abgestürzter Review-Lauf hinterlässt keine Datei — das darf niemals als
    Freigabe durchgehen.
    """
    datei = Path(worktree) / VERDIKT_DATEI
    if not datei.is_file():
        return False, [{"severity": "critical", "what": "kein Review-Urteil hinterlegt"}]
    try:
        daten = json.loads(datei.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, [{"severity": "critical", "what": f"Review-Urteil unlesbar: {exc}"}]

    # json.loads akzeptiert auch nicht-Objekt-Werte auf oberster Ebene (null,
    # eine Liste, ein String, eine Zahl) — gültiges JSON, aber kein Urteil.
    # Ohne diese Prüfung würde daten.get(...) weiter unten mit AttributeError
    # sterben, sobald ein Review-Agent eine leicht falsche JSON-Form schreibt.
    # Derselbe Grundsatz gilt schon für das verschachtelte "findings"-Feld
    # (isinstance-Check unten) — hier wird er nur auf die oberste Ebene erweitert.
    if not isinstance(daten, dict):
        art = type(daten).__name__
        return False, [{"severity": "critical", "what": f"Review-Urteil hat unerwartete Form: {art} statt Objekt"}]

    befunde = daten.get("findings") or []
    if not isinstance(befunde, list):
        return False, [{"severity": "critical", "what": "findings ist keine Liste"}]

    # Einträge, die keine Dicts sind (z.B. eine Liste von Strings statt
    # {"severity": ..., "what": ...}), dürfen lies_verdikt nicht mit einer
    # AttributeError auf .get() zum Absturz bringen — sie zählen einfach nicht
    # als harter Befund.
    hart = [b for b in befunde if isinstance(b, dict) and b.get("severity") in _HARTE_BEFUNDE]
    if hart:
        # Auch wenn der Agent sich selbst 'pass' gegeben hat: der Befund gilt.
        return False, hart
    return daten.get("verdict") == "pass", []


def _geaenderte_dateien(worktree: Path, basis: str) -> list[str]:
    ergebnis = gitctl.run("diff", "--name-only", f"{basis}...HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        return []
    return [z for z in ergebnis.stdout.splitlines() if z.strip()]


def _diff_groesse(worktree: Path, basis: str) -> tuple[int, list[str]]:
    """Zeilenzahl und binäre Dateien aus einem Diff.

    Für binäre Dateien meldet "git diff --numstat" die Spalten als "-" statt
    als Zahlen (Format: "-<TAB>-<TAB><Pfad>").
    "-".isdigit() ist False, also würde eine binäre Datei sonst mit 0 Zeilen
    durchgehen und die MAX_DIFF_ZEILEN-Grenze umgehen können — ausgerechnet in
    diesem Modul, der letzten Prüfung vor einem unbeaufsichtigten Merge (Plan 3).
    Ein großer Blob außerhalb einer Sperrzone dürfte also nie einfach 0 Zeilen
    zählen. Statt ihn zu schätzen (wie viele "Zeilen" hat ein Bild?), bekommt
    er einen eigenen, expliziten Gate-Grund mit Dateinamen.
    """
    ergebnis = gitctl.run("diff", "--numstat", f"{basis}...HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        return 0, []
    summe = 0
    binaere: list[str] = []
    for zeile in ergebnis.stdout.splitlines():
        teile = zeile.split("\t")
        if len(teile) < 2:
            continue
        hinzu, entfernt = teile[0], teile[1]
        if hinzu == "-" and entfernt == "-":
            pfad = teile[2] if len(teile) >= 3 else "<unbekannter Pfad>"
            binaere.append(pfad)
            continue
        for wert in (hinzu, entfernt):
            if wert.isdigit():
                summe += int(wert)
    return summe, binaere


def _tests_pruefen(worktree: Path) -> str | None:
    """Führt die Suite aus. Timeout oder ein fehlendes Interpreter-Binary
    dürfen niemals als Exception aus pruefe() herausschlagen — pruefe() läuft
    im Daemon-Tick, und unter launchd ist PATH nicht die interaktive Shell."""
    try:
        ergebnis = subprocess.run(
            ["python3.14", "-m", "pytest", "-q"], cwd=str(worktree),
            capture_output=True, text=True, timeout=_TEST_TIMEOUT_SEKUNDEN,
        )
    except subprocess.TimeoutExpired:
        return f"Tests rot: Zeitüberschreitung nach {_TEST_TIMEOUT_SEKUNDEN}s"
    except OSError as exc:
        return f"Tests rot: Aufruf fehlgeschlagen ({exc})"
    if ergebnis.returncode != 0:
        letzte_zeile = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout else "ohne Ausgabe"
        return f"Tests rot: {letzte_zeile}"
    return None


def _lint_pruefen(worktree: Path) -> str | None:
    """Wie _tests_pruefen: Timeout und fehlendes Binary werden zu einem Grund,
    nicht zu einer Exception."""
    try:
        ergebnis = subprocess.run(
            ["ruff", "check", "."], cwd=str(worktree),
            capture_output=True, text=True, timeout=_LINT_TIMEOUT_SEKUNDEN,
        )
    except subprocess.TimeoutExpired:
        return f"ruff meldet Befunde: Zeitüberschreitung nach {_LINT_TIMEOUT_SEKUNDEN}s"
    except OSError as exc:
        return f"ruff meldet Befunde: Aufruf fehlgeschlagen ({exc})"
    if ergebnis.returncode != 0:
        letzte_zeile = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout else "ohne Ausgabe"
        return f"ruff meldet Befunde: {letzte_zeile}"
    return None


def pruefe(worktree: Path, basis: str = "main") -> GateErgebnis:
    """Das vollständige Gate. Alle Gründe werden gesammelt, nicht nur der erste —
    ein Bericht mit einem einzigen Grund führt zu einer Fix-Runde, die den Rest
    erst danach findet."""
    gruende: list[str] = []
    baum = Path(worktree)

    dateien = _geaenderte_dateien(baum, basis)
    if not dateien:
        gruende.append("keine Änderungen im Branch")

    verboten = beruehrt_sperrzone(dateien)
    if verboten:
        gruende.append(f"Sperrzone berührt: {', '.join(verboten)}")

    groesse, binaere = _diff_groesse(baum, basis)
    if groesse > MAX_DIFF_ZEILEN:
        gruende.append(f"Diff zu groß: {groesse} Zeilen (Grenze {MAX_DIFF_ZEILEN})")
    if binaere:
        gruende.append(f"binäre Änderung erkannt: {', '.join(binaere)}")

    tests_grund = _tests_pruefen(baum)
    if tests_grund:
        gruende.append(tests_grund)

    lint_grund = _lint_pruefen(baum)
    if lint_grund:
        gruende.append(lint_grund)

    verdikt_ok, befunde = lies_verdikt(baum)
    if not verdikt_ok:
        gruende.append(f"Review-Verdikt negativ: {len(befunde)} harte Befunde")

    return GateErgebnis(ok=not gruende, gruende=gruende)
