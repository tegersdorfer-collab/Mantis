"""Das Gate — die letzte Instanz vor dem Merge.

Bewusst ohne LLM. Alles andere in der Pipeline ist ein Sprachmodell, das sich
selbst beurteilt; hier entscheiden Rückgabewerte. Ein Lauf kann das Gate nicht
überreden, nur bestehen.

Der Merge selbst passiert hier NICHT — das macht forge/freigabe.py nach Timos
Freigabe. Das Gate liefert nur das Urteil.
"""
import json
import logging
import os
import subprocess
import sys
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

# Pfad-Präfixe, in denen die Agenten überhaupt arbeiten dürfen. Bewusst eine
# ERLAUBNIS-, keine Verbotsliste: bei einer Verbotsliste wäre jeder neu
# angelegte Ordner automatisch offen. SPERRZONEN bleibt zusätzlich bestehen —
# eine Datei muss beide Prüfungen bestehen.
#
# `core/skills/` ist die einzige Ausnahme innerhalb von `core/`: dort entstehen
# neue Mantis-Fähigkeiten, und genau diese Arbeit soll der Nachtbetrieb machen.
ERLAUBTE_ZONEN = (
    "tests/",
    "docs/",
    "scripts/",
    "tools/",
    "core/skills/",
)


def ausserhalb_erlaubter_zonen(dateien: list[str]) -> list[str]:
    """Welche der Dateien liegen ausserhalb jeder erlaubten Zone?

    Alle Zonen enden auf "/" und matchen als Präfix. Dass sie auf "/" enden,
    ist die Prüfung gegen Treffer über die Verzeichnisgrenze hinweg:
    "testsuite.py" beginnt mit "tests", aber nicht mit "tests/".
    """
    treffer = []
    for datei in dateien:
        pfad = _normalisiere_pfad(datei)
        if not any(pfad.startswith(zone) for zone in ERLAUBTE_ZONEN):
            treffer.append(datei)
    return treffer


# Darüber ist es kein Task mehr, sondern ein Projekt — dann soll ein Mensch schauen.
MAX_DIFF_ZEILEN = 800

VERDIKT_DATEI = ".forge/review.json"

# Befunde ab dieser Schwere kippen ein 'pass' des Review-Agenten.
_HARTE_BEFUNDE = frozenset({"critical", "important"})

# Zeitbudget für die beiden externen Aufrufe, die pruefe() macht.
_TEST_TIMEOUT_SEKUNDEN = 1800
_LINT_TIMEOUT_SEKUNDEN = 300

# Abschluss-Review 2c, I1: die DATABASE_URL, die die Suite im Gate sieht.
# Das Gate lässt `pytest -q` über die GANZE Suite im Worktree eines Tasks
# laufen — und tests/ ist eine erlaubte Zone, ein Agent darf dort schreiben.
# Ohne Umleitung erbte pytest die Umgebung des Daemons und damit die echte
# Postgres (settings.cfg liest DATABASE_URL mit Vorrang Umgebung > .env >
# Default; ein Wert in der Umgebung gewinnt also auch gegen ~/Mantis/.env,
# geprüft 2026-09-14). Port 1 hört nirgends: die beiden Dateien, die die
# echte DB brauchen (tests/test_forge_lebenszyklus.py, tests/
# test_forge_nachtlauf.py), überspringen sich über `_db_erreichbar()`, und
# jeder agentengeschriebene Test, der nach der DB greift, wird rot statt in
# Produktionstabellen zu schreiben.
GATE_DATABASE_URL = "postgresql://localhost:1/forge-gate-ohne-db"


@dataclass
class GateErgebnis:
    ok: bool
    gruende: list[str] = field(default_factory=list)
    diff: str = ""
    tests_output: str = ""
    lint_output: str = ""


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
    """Alle Pfade, die der Branch anfasst — Quelle UND Ziel einer Umbenennung.

    `--no-renames` ist hier keine Kosmetik, sondern die Prüfung selbst.
    Rename-Erkennung ist in git standardmässig an, und `--name-only` zeigt dann
    nur noch das ZIEL. Gemessen am 2026-09-10 an einem echten Repo, ein Commit,
    der core/db.py nach tools/db.py verschiebt:

        --name-only:              tools/db.py
        --name-only --no-renames: core/db.py
                                  tools/db.py

    Ohne die Option sähe das Gate also nur den Zielpfad, in einer erlaubten
    Zone — und weder Sperrzonen- noch Zonen-Prüfung bekäme die Quelle je zu
    Gesicht. Damit liesse sich jede Datei unsichtbar aus einer Sperrzone
    heraustragen.
    """
    ergebnis = gitctl.run("diff", "--name-only", "--no-renames", f"{basis}...HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        return []
    return [z for z in ergebnis.stdout.splitlines() if z.strip()]


# git-Dateimodus eines Symlinks. Ein Symlink ist im Baum ein Blob, dessen Inhalt
# der Zielpfad ist — sichtbar nur im Modus, nie im Pfadnamen.
_SYMLINK_MODUS = "120000"


def _symlinks_im_diff(worktree: Path, basis: str) -> list[str]:
    """Welche Einträge des Diffs sind am Ende Symlinks?

    Sperrzonen und erlaubte Zonen urteilen über Pfad-ZEICHENKETTEN, nie über
    das, was an einem Pfad tatsächlich liegt. `tests/link -> ../core/db.py`
    besteht deshalb beide Listen: der Pfad liegt in einer erlaubten Zone und in
    keiner Sperrzone — sein Ziel aber im Kern. Da das Gate die letzte Instanz
    vor einem unbeaufsichtigten Merge ist, wird ein Symlink hier gar nicht erst
    beurteilt, sondern abgelehnt.

    `git diff --raw` zeigt beide Dateimodi ("`:100644 120000 <sha> <sha> T`");
    geprüft wird der ZIEL-Modus, also der Zustand nach dem Branch. Ein
    GELÖSCHTER Symlink hat Zielmodus 000000 und ist damit korrekt kein Befund —
    er ist ja weg. `--no-renames` aus demselben Grund wie oben.
    """
    ergebnis = gitctl.run("diff", "--raw", "--no-renames", f"{basis}...HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        return []
    treffer = []
    for zeile in ergebnis.stdout.splitlines():
        if not zeile.startswith(":") or "\t" not in zeile:
            continue
        kopf, _, pfad = zeile.partition("\t")
        felder = kopf.split()
        if len(felder) >= 2 and felder[1] == _SYMLINK_MODUS:
            treffer.append(pfad.strip())
    return treffer


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

    `--no-renames` wie in _geaenderte_dateien: mit Rename-Erkennung meldet
    --numstat eine Verschiebung als "0 0 {core => tools}/db.py". Ein Commit,
    der beliebig viel Code durch die Gegend trägt, zählte damit 0 Zeilen und
    liefe an MAX_DIFF_ZEILEN vorbei. Ohne die Option stehen Löschung und
    Neuanlage mit ihren echten Zeilenzahlen da (gemessen 2026-09-10: 0/3 und
    3/0 statt 0/0).
    """
    ergebnis = gitctl.run("diff", "--numstat", "--no-renames", f"{basis}...HEAD", cwd=worktree)
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


# Abschluss-Review 2c, C1: pytest und ruff laufen über den Interpreter, in
# dem das Gate selbst läuft — nie über einen nackten Binärnamen. Unter dem
# launchd-PATH der plist (~/.local/bin, /opt/homebrew/bin, /usr/bin, …) war
# weder `python3.14` noch `ruff` auflösbar; das Journal vom 2026-08-15 zeigt
# genau diesen Ausfall ("Tests rot: Aufruf fehlgeschlagen"). sys.executable
# existiert per Definition, und `python3.14 -m ruff` funktioniert, weil ruff
# als Paket im selben Interpreter installiert ist (geprüft 2026-09-14:
# ruff 0.15.20).
_PYTEST_ARGV = [sys.executable, "-m", "pytest", "-q"]
_RUFF_ARGV = [sys.executable, "-m", "ruff", "check", "."]


def _ausgabe_text(stdout: str | bytes | None, stderr: str | bytes | None = None) -> str:
    """Vereinheitlicht auch TimeoutExpired-Ausgaben für optionale Traces."""
    teile = []
    for wert in (stdout, stderr):
        if isinstance(wert, bytes):
            wert = wert.decode("utf-8", errors="replace")
        if wert:
            teile.append(str(wert))
    return "\n".join(teile)


def _tests_ausfuehren(worktree: Path) -> tuple[str | None, str]:
    """Führt die Suite aus. Timeout oder ein fehlendes Interpreter-Binary
    dürfen niemals als Exception aus pruefe() herausschlagen — pruefe() läuft
    im Daemon-Tick, und unter launchd ist PATH nicht die interaktive Shell."""
    # Siehe GATE_DATABASE_URL: die Suite im Task-Worktree darf die
    # Produktions-DB nicht sehen. Alles andere aus der Umgebung bleibt.
    umgebung = {**os.environ, "DATABASE_URL": GATE_DATABASE_URL}
    try:
        ergebnis = subprocess.run(
            _PYTEST_ARGV, cwd=str(worktree), env=umgebung,
            capture_output=True, text=True, timeout=_TEST_TIMEOUT_SEKUNDEN,
        )
    except subprocess.TimeoutExpired as exc:
        return (f"Tests rot: Zeitüberschreitung nach {_TEST_TIMEOUT_SEKUNDEN}s",
                _ausgabe_text(exc.stdout, exc.stderr))
    except OSError as exc:
        return f"Tests rot: Aufruf fehlgeschlagen ({exc})", str(exc)
    output = _ausgabe_text(ergebnis.stdout, ergebnis.stderr)
    if ergebnis.returncode != 0:
        letzte_zeile = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout else "ohne Ausgabe"
        return f"Tests rot: {letzte_zeile}", output
    return None, output


def _tests_pruefen(worktree: Path) -> str | None:
    """Kompatibler Wrapper für Aufrufer, die nur den Fehlergrund brauchen."""
    return _tests_ausfuehren(worktree)[0]


def _lint_ausfuehren(worktree: Path) -> tuple[str | None, str]:
    """Wie _tests_pruefen: Timeout und fehlendes Binary werden zu einem Grund,
    nicht zu einer Exception."""
    try:
        ergebnis = subprocess.run(
            _RUFF_ARGV, cwd=str(worktree),
            capture_output=True, text=True, timeout=_LINT_TIMEOUT_SEKUNDEN,
        )
    except subprocess.TimeoutExpired as exc:
        return (f"ruff meldet Befunde: Zeitüberschreitung nach {_LINT_TIMEOUT_SEKUNDEN}s",
                _ausgabe_text(exc.stdout, exc.stderr))
    except OSError as exc:
        return f"ruff meldet Befunde: Aufruf fehlgeschlagen ({exc})", str(exc)
    output = _ausgabe_text(ergebnis.stdout, ergebnis.stderr)
    if ergebnis.returncode != 0:
        letzte_zeile = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout else "ohne Ausgabe"
        return f"ruff meldet Befunde: {letzte_zeile}", output
    return None, output


def _lint_pruefen(worktree: Path) -> str | None:
    """Kompatibler Wrapper für Aufrufer, die nur den Fehlergrund brauchen."""
    return _lint_ausfuehren(worktree)[0]


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

    draussen = ausserhalb_erlaubter_zonen(dateien)
    if draussen:
        gruende.append(f"ausserhalb der erlaubten Zonen: {', '.join(draussen)}")

    # Beide Listen oben urteilen über Pfad-Zeichenketten. Ein Symlink ist genau
    # die Lücke darin: sein Pfad kann in einer erlaubten Zone liegen, sein Ziel
    # überall. Deshalb wird er nicht beurteilt, sondern abgelehnt.
    symlinks = _symlinks_im_diff(baum, basis)
    if symlinks:
        gruende.append(f"Symlink im Diff: {', '.join(symlinks)}")

    groesse, binaere = _diff_groesse(baum, basis)
    if groesse > MAX_DIFF_ZEILEN:
        gruende.append(f"Diff zu groß: {groesse} Zeilen (Grenze {MAX_DIFF_ZEILEN})")
    if binaere:
        gruende.append(f"binäre Änderung erkannt: {', '.join(binaere)}")

    verdikt_ok, befunde = lies_verdikt(baum)
    if not verdikt_ok:
        gruende.append(f"Review-Verdikt negativ: {len(befunde)} harte Befunde")

    diff_ergebnis = gitctl.run("diff", "--no-ext-diff", f"{basis}...HEAD", cwd=baum)
    diff = (diff_ergebnis.stdout or "") if diff_ergebnis.returncode == 0 else ""

    # Abschluss-Review 2c, I1: ein Diff, der eine Sperrzone berührt oder die
    # erlaubten Zonen verlässt, ist durch nichts mergefähig, was die Tests
    # beweisen könnten. Die billigen Gründe oben stehen trotzdem alle im
    # Bericht; nur die teuren Läufe (bis zu 30 min pytest, dazu ruff)
    # entfallen — und mit ihnen ein Lauf agentengeschriebener Tests für einen
    # ohnehin verlorenen Task.
    if verboten or draussen:
        return GateErgebnis(ok=False, gruende=gruende, diff=diff)

    tests_grund, tests_output = _tests_ausfuehren(baum)
    if tests_grund:
        gruende.append(tests_grund)

    lint_grund, lint_output = _lint_ausfuehren(baum)
    if lint_grund:
        gruende.append(lint_grund)

    return GateErgebnis(
        ok=not gruende,
        gruende=gruende,
        diff=diff,
        tests_output=tests_output,
        lint_output=lint_output,
    )
