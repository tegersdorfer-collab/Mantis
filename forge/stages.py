"""Die fünf Pipeline-Stufen.

Jede Stufe ist ein eigener headless Lauf mit frischem Kontext. Die Übergabe
läuft über Artefakte im Worktree, nie über Gesprächsverlauf — deshalb steht in
jedem Prompt, wo das Ergebnis der Vorstufe liegt, statt es mitzuschicken.

Die Rechteprofile sind die einzige Schranke zwischen einem unbeaufsichtigten
Agenten und dem Dateisystem. Sie sind absichtlich eng: nur `implement` und
`fix` dürfen editieren und Befehle ausführen (`Edit`, `Bash`). `spec`, `plan`
und `review` bekommen `Write`, aber NICHT bepfadet. Diese drei Stufen könnten
also grundsätzlich jede Datei im Worktree anlegen oder überschreiben,
einschließlich forge/gate.py. Der eigentliche Schutz dagegen ist zweifach:
(1) keine der drei Stufen hat `Edit` oder `Bash` — sie können also nur neue
Dateien anlegen, keine bestehenden gezielt verändern und keine Befehle
ausführen; (2) das deterministische Gate (forge/gate.py, Sperrzonen-Prüfung,
siehe Plan-Task 5) bewertet den entstandenen Diff nach dem Lauf und weist
Änderungen an gesperrten Pfaden zurück, unabhängig davon, welche Stufe sie
verursacht hat. Das Rechteprofil ist die erste, das Gate die zweite und
verlässlichere Schranke.

Fehlgeschlagener Versuch (2026-08-14): `Write(<pfad>/**)` sollte das Write
dieser drei Stufen auf ihr eigenes Artefaktverzeichnis beschränken (Probe (d)
in tests/fixtures/permission_probe.md). Zwei weitere Proben gegen die echte
CLI (2.1.126) zeigen, dass ein Write INNERHALB des angegebenen Musters
ebenfalls verweigert wird (Proben (e) und (f) ebendort) —
`Write(<muster>)` verweigert in dieser CLI-Version grundsätzlich jeden
Schreibzugriff, unabhängig vom Pfad. Die frühere Schlussfolgerung aus Probe
(d) war ein Fehlschluss: sie hatte nur belegt, dass ein Write AUSSERHALB des
Musters verweigert wird — das ist mit "das Muster grenzt korrekt ein" genauso
vereinbar wie mit "Write(<muster>) verweigert grundsätzlich alles", und es
war Letzteres. Nur bare `Write` (siehe Probe (a)) funktioniert. Deshalb unten
bare `Write` — NICHT wieder auf ein Pfad-Muster umstellen, ohne eine neue
Messung, die das Gegenteil zeigt.
"""
from dataclasses import dataclass
from typing import Callable

from forge import models as m
from forge import prompts
from forge.runner import PermissionProfile

# Wo die Stufen ihre Artefakte ablegen — relativ zum Worktree.
SPEC_VERZEICHNIS = "docs/superpowers/specs"
PLAN_VERZEICHNIS = "docs/superpowers/plans"
VERDIKT_DATEI = ".forge/review.json"

# Die Review-Stufe hat absichtlich kein Bash und kann sich also KEINEN eigenen
# Diff erzeugen (kein `git diff`, kein sonstiger Befehl) — sonst könnte sie
# sich eine ihr genehme Sicht auf die Änderung zusammenbauen, statt die
# tatsächliche zu prüfen. Stattdessen liest sie den Diff aus dieser Datei.
# WICHTIG: forge/pipeline.py (Task 6) MUSS diese Datei schreiben, BEVOR die
# Review-Stufe läuft — ohne sie prüft review eine Spec ohne jede Sicht auf
# das, was tatsächlich geändert wurde, und das Gate-Urteil stünde auf nichts.
DIFF_DATEI = ".forge/diff.patch"


@dataclass(frozen=True)
class Stage:
    name: str
    state: str
    next_state: str
    profile: PermissionProfile
    baue_prompt: Callable[[dict, dict], str]
    artefakt: Callable[[dict], str | None]


def _kopf(task: dict) -> str:
    return (
        "Du arbeitest unbeaufsichtigt in einem isolierten git-Worktree des "
        "Mantis-Projekts (lokaler AI-Concierge, Python 3.14, FastAPI, PostgreSQL).\n"
        "Niemand kann dir Rückfragen beantworten. Wo etwas unklar ist, triff eine "
        "Annahme, schreibe sie ausdrücklich als 'Annahme:' hin und arbeite weiter.\n\n"
        + prompts.aufgabenblock(task)
    )


def _spec_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Schreibe eine Design-Spec nach {SPEC_VERZEICHNIS}/. Dateiname: "
        f"YYYY-MM-DD-<kurzer-slug>-design.md.\n"
        "Inhalt: Ziel, betroffene Module, Datenfluss, Fehlerbehandlung, was "
        "ausdrücklich NICHT gebaut wird, und alle getroffenen Annahmen.\n"
        "Ändere sonst nichts. Antworte am Ende nur mit dem Pfad der Datei."
    )


def _plan_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Die Design-Spec liegt unter: {kontext.get('spec_path', '(unbekannt)')}\n"
        f"Lies sie und schreibe daraus einen Implementierungsplan nach {PLAN_VERZEICHNIS}/. "
        "Dateiname: YYYY-MM-DD-<kurzer-slug>-plan.md.\n"
        "Zerlege in Schritte mit je einem eigenständig testbaren Ergebnis. Jeder "
        "Schritt nennt die exakten Dateipfade und enthält den Test ZUERST.\n"
        "Ändere sonst nichts. Antworte am Ende nur mit dem Pfad der Datei."
    )


def _implement_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Spec: {kontext.get('spec_path', '(unbekannt)')}\n"
        f"Plan: {kontext.get('plan_path', '(unbekannt)')}\n"
        "Arbeite den Plan testgetrieben ab: erst der fehlschlagende Test, dann "
        "die Implementierung, dann committen. Halte dich an die Konventionen der "
        "Codebase (Kommentare auf Deutsch, ruff select F+E9, line-length 120).\n"
        "Fasse NICHT an: .env, data/, forge/gate.py, forge/runner.py.\n"
        "Committe deine Arbeit mit expliziten Pfaden, niemals mit 'git add -A'."
    )


def _review_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Die Spec liegt unter: {kontext.get('spec_path', '(unbekannt)')}\n"
        f"Der Diff dieses Branches gegen die Spec liegt unter: {DIFF_DATEI}. Das "
        "ist deine einzige verlässliche Sicht auf die Änderung — lies diese "
        "Datei, statt einen eigenen Diff zu erzeugen. Du hast den "
        "Entstehungsverlauf NICHT gesehen und sollst ihm auch nicht vertrauen.\n"
        f"Schreibe dein Urteil als JSON nach {VERDIKT_DATEI}:\n"
        '{"verdict": "pass" oder "fail", "findings": [{"severity": "critical|important|minor", '
        '"file": "...", "what": "..."}]}\n'
        "verdict ist 'fail', sobald mindestens ein Befund critical oder important ist.\n"
        "Ändere ausschließlich diese eine Datei."
    )


def _fix_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Ein Review hat Mängel gefunden. Sie stehen in {VERDIKT_DATEI}.\n"
        "Behebe ausschließlich die dort als critical oder important markierten "
        "Befunde. Baue nichts darüber hinaus. Lass die Tests danach laufen und "
        "committe mit expliziten Pfaden.\n"
        "Fasse NICHT an: .env, data/, forge/gate.py, forge/runner.py."
    )


# Die Kette: vier Stufen, lückenlos von speccing bis gating.
STAGES: tuple[Stage, ...] = (
    Stage("spec", m.SPECCING, m.PLANNING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _spec_prompt, lambda t: t.get("spec_path")),
    Stage("plan", m.PLANNING, m.IMPLEMENTING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _plan_prompt, lambda t: t.get("plan_path")),
    Stage("implement", m.IMPLEMENTING, m.REVIEWING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
                            mode="dontAsk"),
          _implement_prompt, lambda t: None),
    Stage("review", m.REVIEWING, m.GATING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _review_prompt, lambda t: VERDIKT_DATEI),
)

# Die Fix-Stufe steht bewusst NEBEN der Kette, nicht darin: sie teilt sich den
# Zustand `reviewing` mit der Review-Stufe und wird von der Pipeline gezielt
# angefordert, wenn ein Verdikt negativ war. Stünde sie in STAGES, wäre die
# Kette nicht mehr eindeutig über den Zustand auflösbar.
FIX_STAGE = Stage(
    "fix", m.REVIEWING, m.GATING,
    PermissionProfile(allowed=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
                      mode="dontAsk"),
    _fix_prompt, lambda t: None,
)

ALLE_STUFEN: tuple[Stage, ...] = STAGES + (FIX_STAGE,)


def fuer_state(state: str) -> Stage | None:
    """Die Kettenstufe zu diesem Zustand. FIX_STAGE ist absichtlich nicht
    erreichbar — die Pipeline fordert sie direkt an."""
    for stufe in STAGES:
        if stufe.state == state:
            return stufe
    return None
