"""Die fünf Pipeline-Stufen.

Jede Stufe ist ein eigener headless Lauf mit frischem Kontext. Die Übergabe
läuft über Artefakte im Worktree, nie über Gesprächsverlauf — deshalb steht in
jedem Prompt, wo das Ergebnis der Vorstufe liegt, statt es mitzuschicken.

ACHTUNG, seit der Umstellung auf die Gratis-Backends (Plan 1, 2026-09-09):
die `profile`-Felder unten gelten NUR für das Claude-Code-Backend in
forge/runner.py. Die Pipeline ruft dieses Backend derzeit nicht auf — sie geht
über forge/backends.hole(), und dort kommen die Rechte aus
forge.backends.OpencodePermission, für ALLE Stufen gleich (`bash`, `task`,
`webfetch`, `websearch`, `external_directory` verboten; lesen und schreiben
erlaubt). Die Profile hier sind damit produktiv wirkungslos; sie bleiben
stehen, weil sie bei der Stage-Konstruktion weiterhin die gemessenen
Claude-Code-Schranken erzwingen (siehe Ruling Task 7 im SDD-Ledger). Der
folgende Absatz beschreibt entsprechend den Claude-Code-Fall, nicht den
laufenden Betrieb.

Die Rechteprofile waren die erste Schranke zwischen einem unbeaufsichtigten
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

from forge import gate
from forge import models as m
from forge import prompts
from forge.runner import PermissionProfile

# Wo die Stufen ihre Artefakte ablegen — relativ zum Worktree.
SPEC_VERZEICHNIS = "docs/superpowers/specs"
PLAN_VERZEICHNIS = "docs/superpowers/plans"
VERDIKT_DATEI = ".forge/review.json"

# Die Review-Stufe soll sich KEINEN eigenen Diff erzeugen — sonst könnte sie
# sich eine ihr genehme Sicht auf die Änderung zusammenbauen, statt die
# tatsächliche zu prüfen. Deshalb der Umweg über diese Datei: forge/pipeline.py
# schreibt sie, und forge/runner_agy.py liest sie und legt ihren Inhalt dem
# Modell in den Prompt. Das Modell selbst wird nie auf diesen Pfad verwiesen
# (I7) — es bekommt den Diff fertig eingebettet.
# WICHTIG: forge/pipeline.py MUSS diese Datei schreiben, BEVOR die
# Review-Stufe läuft — ohne sie prüft review eine Spec ohne jede Sicht auf
# das, was tatsächlich geändert wurde, und das Gate-Urteil stünde auf nichts.
DIFF_DATEI = ".forge/diff.patch"


# Zeitbudget für die reinen Lese-/Schreib-Stufen (spec, plan, review): sie
# erzeugen genau ein Dokument bzw. lesen eine Diff-Datei und urteilen — 1800s
# waren dafür in der Praxis (Akzeptanzlauf 2026-08-14) reichlich.
STANDARD_TIMEOUT_SEKUNDEN = 1800

# implement/fix arbeiten testgetrieben über mehrere Dateien: fehlschlagenden
# Test schreiben, Implementierung, Tests der ganzen Suite laufen lassen (1142+
# Fälle), committen — oft in mehreren Runden. Der Akzeptanzlauf vom
# 2026-08-14 zeigt genau das Muster, das eine einheitliche Grenze bricht: die
# Stufe hatte ihre Arbeit bereits fertig UND committet, lief aber trotzdem in
# die 1800s-Grenze, weil das Modell danach noch weiterarbeitete (z.B. eigene
# Verifikation). Das Dreifache des Lese-Budgets gibt genug Luft für mehrere
# Test-Läufe der vollen Suite, ohne einen echt hängenden Lauf endlos offen zu
# lassen — 1800s bleiben für die drei Lese-Stufen unverändert, weil dort
# dieselbe Begründung nicht zutrifft (kein Testlauf, kein Mehrdateien-Umbau).
IMPLEMENT_TIMEOUT_SEKUNDEN = 5400


@dataclass(frozen=True)
class Stage:
    name: str
    state: str
    next_state: str
    profile: PermissionProfile
    baue_prompt: Callable[[dict, dict], str]
    artefakt: Callable[[dict], str | None]
    timeout: int = STANDARD_TIMEOUT_SEKUNDEN
    # Welche CLI diese Stufe ausführt und mit welchem Modell. `profile` bleibt
    # erhalten und gilt weiterhin für das Claude-Code-Backend; die opencode-
    # Rechte kommen aus forge/backends.OpencodePermission.
    backend: str = "opencode"
    model: str = ""


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


def _zonen_satz() -> str:
    """Abschluss-Review 2c, I5: implement und fix nennen die erlaubten Zonen.
    Die Liste kommt zur Laufzeit aus gate.ERLAUBTE_ZONEN — eine Kopie hier
    würde beim nächsten Zonen-Umbau stillschweigend veralten, und ein Agent,
    dem die Zonen niemand sagt, verbrennt einen ganzen Zyklus, um sie am Gate
    herauszufinden."""
    zonen = list(gate.ERLAUBTE_ZONEN)
    aufzaehlung = ", ".join(zonen[:-1]) + " und " + zonen[-1] if len(zonen) > 1 else zonen[0]
    return (f"Arbeite ausschließlich unter {aufzaehlung} — Änderungen außerhalb "
            "weist das Gate zurück.")


def _implement_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Spec: {kontext.get('spec_path', '(unbekannt)')}\n"
        f"Plan: {kontext.get('plan_path', '(unbekannt)')}\n"
        "Arbeite den Plan testgetrieben ab: erst der fehlschlagende Test, dann "
        "die Implementierung. Halte dich an die Konventionen der "
        "Codebase (Kommentare auf Deutsch, ruff select F+E9, line-length 120).\n"
        "Fasse NICHT an: .env, data/, forge/gate.py, forge/runner.py.\n"
        + _zonen_satz() + "\n"
        "Du hast keine Shell: keine Befehle, kein git, keine Testläufe. Lass "
        "deine Arbeit einfach im Worktree stehen — die Pipeline committet sie."
    )


def _review_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Die Spec liegt unter: {kontext.get('spec_path', '(unbekannt)')}\n"
        "Der Diff dieses Branches steht weiter unten in diesem Prompt — er ist "
        "deine einzige verlässliche Sicht auf die Änderung. Erzeuge keinen "
        "eigenen Diff und suche keine Datei danach ab. Du hast den "
        "Entstehungsverlauf NICHT gesehen und sollst ihm auch nicht vertrauen.\n"
        "Gib dein Urteil als JSON auf der Standardausgabe aus. Schreibe keine "
        f"Datei — {VERDIKT_DATEI} legt der Runner aus deiner Antwort an.\n"
        "Das exakte Schema hängt der Runner unten an den Prompt an; halte dich "
        "wörtlich daran."
    )


def _fix_prompt(task: dict, kontext: dict) -> str:
    return (
        _kopf(task) + "\n\n"
        f"Ein Review hat Mängel gefunden. Sie stehen in {VERDIKT_DATEI}.\n"
        "Behebe ausschließlich die dort als critical oder important markierten "
        "Befunde. Baue nichts darüber hinaus.\n"
        "Fasse NICHT an: .env, data/, forge/gate.py, forge/runner.py.\n"
        + _zonen_satz() + "\n"
        "Du hast keine Shell: keine Befehle, kein git, keine Testläufe. Lass "
        "deine Arbeit einfach im Worktree stehen — die Pipeline committet sie."
    )


# Die Kette: vier Stufen, lückenlos von speccing bis gating.
STAGES: tuple[Stage, ...] = (
    Stage("spec", m.SPECCING, m.PLANNING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _spec_prompt, lambda t: t.get("spec_path"),
          backend="opencode", model="google/gemini-3.6-flash"),
    Stage("plan", m.PLANNING, m.IMPLEMENTING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _plan_prompt, lambda t: t.get("plan_path"),
          backend="opencode", model="nvidia/moonshotai/kimi-k3"),
    Stage("implement", m.IMPLEMENTING, m.REVIEWING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
                            mode="dontAsk"),
          _implement_prompt, lambda t: None, timeout=IMPLEMENT_TIMEOUT_SEKUNDEN,
          backend="opencode", model="nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
    Stage("review", m.REVIEWING, m.GATING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _review_prompt, lambda t: VERDIKT_DATEI,
          backend="agy", model="claude-opus-4-6-thinking"),
)

# Die Fix-Stufe steht bewusst NEBEN der Kette, nicht darin: sie teilt sich den
# Zustand `reviewing` mit der Review-Stufe und wird von der Pipeline gezielt
# angefordert, wenn ein Verdikt negativ war. Stünde sie in STAGES, wäre die
# Kette nicht mehr eindeutig über den Zustand auflösbar.
FIX_STAGE = Stage(
    "fix", m.REVIEWING, m.GATING,
    PermissionProfile(allowed=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
                      mode="dontAsk"),
    _fix_prompt, lambda t: None, timeout=IMPLEMENT_TIMEOUT_SEKUNDEN,
    backend="opencode", model="nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
)

ALLE_STUFEN: tuple[Stage, ...] = STAGES + (FIX_STAGE,)


def fuer_state(state: str) -> Stage | None:
    """Die Kettenstufe zu diesem Zustand. FIX_STAGE ist absichtlich nicht
    erreichbar — die Pipeline fordert sie direkt an."""
    for stufe in STAGES:
        if stufe.state == state:
            return stufe
    return None
