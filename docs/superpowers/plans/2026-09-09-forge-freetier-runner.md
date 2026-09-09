# Forge-Runner auf Gratis-Anbietern — Implementierungsplan (Plan 1 von 4)

> **Für agentische Worker:** ERFORDERLICHE SUB-SKILL: Nutze
> superpowers:subagent-driven-development (empfohlen) oder
> superpowers:executing-plans, um diesen Plan Task für Task umzusetzen.
> Die Schritte nutzen Checkbox-Syntax (`- [ ]`) zur Nachverfolgung.

**Ziel:** Die bestehenden Forge-Stufen laufen wahlweise auf `opencode`
(NVIDIA/Google/Groq) oder `agy` (Antigravity) statt auf Claude Code — bei
unverändertem Verhalten der Pipeline.

**Architektur:** `forge/runner.py` bleibt unangetastet. Eine Backend-Abstraktion
mit derselben Signatur wie `runner.run` tritt an die eine Stelle, an der die
Pipeline heute den Runner aufruft (`forge/pipeline.py:439`). `Stage` erhält
`backend` und `model`; welches Backend eine Stufe benutzt, ist damit Daten und
keine Verzweigung im Code.

**Tech-Stack:** Python 3.14, `subprocess`, `json`, pytest mit `monkeypatch`.
Keine neuen Abhängigkeiten.

**Spec:** [../specs/2026-09-09-forge-freetier-design.md](../specs/2026-09-09-forge-freetier-design.md)

## Global Constraints

- Python 3.14. Der Gate ruft `python3.14 -m pytest` (`forge/gate.py:157`).
- Keine neuen Pip-Abhängigkeiten.
- Kommentare, Docstrings und Testnamen auf Deutsch, wie im übrigen `forge/`.
- Tests laufen ohne Datenbank, ohne Netz und ohne echte CLI — `subprocess.run`
  wird gemockt, Ereignisströme kommen aus Fixtures.
- Die Rechtekonfiguration für opencode ist **exakt** die in
  `tests/fixtures/permission_probe_opencode.md` Probe (j) belegte. Jede
  Abweichung Richtung mehr Rechte ist ein Fehler, kein Feature.
- Nach jedem Task: `python3.14 -m pytest tests/ -q` muss grün sein bis auf die
  zwei vorbestehenden Fehlschläge in `tests/test_api_auth.py`
  (WebSocket-Subprotokoll, unabhängig von diesem Plan).

## Dateistruktur

| Datei | Zuständigkeit |
|---|---|
| `forge/backends.py` (neu) | `OpencodePermission`, `Backend`-Protokoll, Registry |
| `forge/runner_opencode.py` (neu) | Config bauen, `opencode run` aufrufen, Events parsen |
| `forge/runner_agy.py` (neu) | `agy` aufrufen, Diff einbetten, Verdikt-Datei schreiben |
| `forge/stages.py` (ändern) | `Stage` um `backend`/`model`; Backends je Stufe zuordnen |
| `forge/pipeline.py:439` (ändern) | Aufruf über die Registry statt direkt `runner.run` |
| `tests/test_forge_backends.py` (neu) | Rechteschranke |
| `tests/test_forge_runner_opencode.py` (neu) | Config und Event-Parsing |
| `tests/test_forge_runner_agy.py` (neu) | Diff-Einbettung, Verdikt-Datei |

## Nicht in diesem Plan

Ausweichketten und die Regel „Reviewer ≠ Implementierer unter Erschöpfung"
gehören zu Plan 2 (Budget), weil sie ohne Erschöpfungsverwaltung keine Wirkung
haben. Hier bekommt jede Stufe **genau ein** festes Modell; dass Review und
Implement verschiedene Modelle sind, prüft Task 6 statisch.

Ebenfalls nicht hier: Mistral für Commit-Texte (Plan 2, gehört zum Merge),
Parallelität und `AWAITING_APPROVAL` (Plan 2), Scout und Telegram-Bot (Plan 3),
Dashboard (Plan 4).

Der Provider `mistral` steht trotzdem schon in der Lauf-Config (Task 2), damit
Plan 2 ihn nicht nachrüsten muss — benutzt wird er hier von keiner Stufe.

---

### Task 1: Rechteschranke für opencode

Die einzige Schranke zwischen einem unbeaufsichtigten Agenten und dem
Dateisystem. Sie wird als Datentyp gebaut, der falsche Werte gar nicht erst
annimmt — nach demselben Muster wie `_ERLAUBTE_MODI` in `forge/runner.py`.

**Files:**
- Create: `forge/backends.py`
- Test: `tests/test_forge_backends.py`

**Interfaces:**
- Consumes: nichts
- Produces: `OpencodePermission()` (frozen dataclass, keine Argumente nötig),
  Methode `.als_dict() -> dict[str, str]`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Tests für die Rechteschranke der opencode-Backends."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge.backends import OpencodePermission


class TestOpencodePermission:
    def test_standard_entspricht_der_aufnahme(self):
        """Probe (j) aus tests/fixtures/permission_probe_opencode.md."""
        d = OpencodePermission().als_dict()
        assert d == {
            "read": "allow", "edit": "allow", "glob": "allow",
            "grep": "allow", "list": "allow",
            "bash": "deny", "task": "deny",
            "webfetch": "deny", "websearch": "deny",
            "external_directory": "deny",
        }

    def test_bash_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(d\\)"):
            OpencodePermission(bash="allow")

    def test_task_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(e\\)"):
            OpencodePermission(task="allow")

    def test_external_directory_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(d\\)"):
            OpencodePermission(external_directory="allow")

    def test_unbekannter_wert_wird_verweigert(self):
        with pytest.raises(ValueError, match="allow.*ask.*deny"):
            OpencodePermission(read="vielleicht")
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_backends.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'forge.backends'`

- [ ] **Schritt 3: Minimale Implementierung**

```python
"""Backends für Forge-Läufe: welche CLI eine Stufe ausführt und mit welchen Rechten.

`forge/runner.py` (Claude Code) bleibt unverändert bestehen. Dieses Modul
ergänzt Backends für `opencode` und `agy` und die Schranke, die deren Rechte
festnagelt.
"""
from dataclasses import dataclass

# Werte, die opencode für eine Rechte-Kategorie akzeptiert.
_AKTIONEN = frozenset({"allow", "ask", "deny"})

# Kategorien, die NIEMALS erlaubt werden dürfen, mit der Aufnahme als Begründung.
# Quelle je Eintrag: tests/fixtures/permission_probe_opencode.md.
_VERBOTEN = {
    "bash": (
        "Probe (d), 2026-09-09: external_directory ist Pfad-Erkennung auf "
        "Shell-Argument-Ebene, kein Sandbox. Ein Pfad, der erst im Zielprozess "
        "entsteht (python3 -c \"open(chr(46)+...)\"), bricht aus und hat den "
        "Köder preisgegeben. bash=allow bedeutet damit keine Eingrenzung."
    ),
    "task": (
        "Probe (e), 2026-09-09: ein Subagent führt Befehle mit eigenen Rechten "
        "aus und umgeht damit die Bash-Beschränkung des Elternagenten. "
        "'whoami' lief trotz Whitelist, delegiert an den General Agent."
    ),
    "external_directory": (
        "Probe (d), 2026-09-09: siehe bash. Diese Kategorie ist die einzige "
        "Schranke gegen Lesezugriff ausserhalb des Worktrees und wird nicht "
        "gelockert."
    ),
}


@dataclass(frozen=True)
class OpencodePermission:
    """Die für unbeaufsichtigten Betrieb belegte Rechtekonfiguration.

    Die Defaults sind Probe (j) aus tests/fixtures/permission_probe_opencode.md.
    Die drei Kategorien in `_VERBOTEN` lassen sich nicht auf 'allow' setzen —
    nicht weil es unklug wäre, sondern weil eine Aufnahme belegt, dass die
    Schranke dann nicht hält. Eine Lockerung gehört erst dann hierher, wenn eine
    neue Aufnahme das Gegenteil zeigt.
    """
    read: str = "allow"
    edit: str = "allow"
    glob: str = "allow"
    grep: str = "allow"
    list: str = "allow"
    bash: str = "deny"
    task: str = "deny"
    webfetch: str = "deny"
    websearch: str = "deny"
    external_directory: str = "deny"

    def __post_init__(self):
        for feld, wert in self.als_dict().items():
            if wert not in _AKTIONEN:
                raise ValueError(
                    f"Ungültige Rechte-Aktion für {feld!r}: {wert!r} — "
                    f"erlaubt sind allow, ask, deny"
                )
            if wert != "deny" and feld in _VERBOTEN:
                raise ValueError(
                    f"Rechte-Kategorie {feld!r} darf nicht {wert!r} sein: "
                    f"{_VERBOTEN[feld]}"
                )

    def als_dict(self) -> dict[str, str]:
        """Die Konfiguration, wie opencode sie unter `agent.<name>.permission` erwartet."""
        return {
            "read": self.read, "edit": self.edit, "glob": self.glob,
            "grep": self.grep, "list": self.list,
            "bash": self.bash, "task": self.task,
            "webfetch": self.webfetch, "websearch": self.websearch,
            "external_directory": self.external_directory,
        }
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_backends.py -q`
Erwartet: PASS, 5 Tests

- [ ] **Schritt 5: Committen**

```bash
git add forge/backends.py tests/test_forge_backends.py
git commit -m "feat(forge): Rechteschranke für opencode-Backends

Nagelt die in Probe (j) belegte Konfiguration fest. bash, task und
external_directory lassen sich nicht auf allow setzen — mit der Aufnahme
als Begründung im Fehlertext."
```

---

### Task 2: opencode-Config je Lauf bauen

Reine Logik ohne I/O, deshalb eigener Task: die Config ist die Stelle, an der
Rechte, Modell und Provider zusammenkommen, und sie muss ohne CLI testbar sein.

**Files:**
- Create: `forge/runner_opencode.py`
- Test: `tests/test_forge_runner_opencode.py`

**Interfaces:**
- Consumes: `OpencodePermission` aus Task 1
- Produces: `baue_config(agent: str, model: str) -> dict`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Tests für das opencode-Backend."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.runner_opencode import baue_config


class TestConfig:
    def test_agent_und_modell_stehen_drin(self):
        c = baue_config("implement", "nvidia/minimaxai/minimax-m3")
        assert c["agent"]["implement"]["model"] == "nvidia/minimaxai/minimax-m3"
        assert c["model"] == "nvidia/minimaxai/minimax-m3"

    def test_rechte_kommen_aus_der_schranke(self):
        c = baue_config("spec", "google/gemini-3.6-flash")
        rechte = c["agent"]["spec"]["permission"]
        assert rechte["bash"] == "deny"
        assert rechte["task"] == "deny"
        assert rechte["external_directory"] == "deny"
        assert rechte["edit"] == "allow"

    def test_schluessel_kommen_aus_der_umgebung_nicht_im_klartext(self):
        """Keys stehen als {env:...}-Verweis drin, nie als Wert."""
        c = baue_config("plan", "nvidia/moonshotai/kimi-k3")
        for provider in c["provider"].values():
            assert provider["options"]["apiKey"].startswith("{env:")

    def test_alle_genutzten_provider_sind_konfiguriert(self):
        c = baue_config("spec", "google/gemini-3.6-flash")
        assert set(c["provider"]) == {"nvidia", "google", "groq", "mistral"}
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'forge.runner_opencode'`

- [ ] **Schritt 3: Minimale Implementierung**

```python
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
from forge.backends import OpencodePermission

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
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py -q`
Erwartet: PASS, 4 Tests

- [ ] **Schritt 5: Committen**

```bash
git add forge/runner_opencode.py tests/test_forge_runner_opencode.py
git commit -m "feat(forge): opencode-Config je Lauf bauen

Eigene Config pro Lauf statt der globalen des Benutzers, Schlüssel nur
als {env:...}-Verweis."
```

---

### Task 3: opencode-Ereignisstrom auswerten

Gegen die echten Aufnahmen `tests/fixtures/opencode_stream_success.jsonl` und
`opencode_stream_denied.jsonl` (aufgenommen 2026-09-09, opencode 1.18.20).

Wichtig für Plan 2: Tokens stehen **je `step_finish`** und müssen über alle
Schritte summiert werden — ein Lauf hat mehrere.

**Files:**
- Modify: `forge/runner_opencode.py`
- Test: `tests/test_forge_runner_opencode.py`

**Interfaces:**
- Consumes: `RunResult` aus `forge/runner.py`
- Produces: `parse_events(lines: Iterable[str]) -> RunResult`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_runner_opencode.py` anhängen:

```python
import pathlib

from forge.runner_opencode import parse_events

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TestEreignisse:
    def test_erfolgreicher_lauf(self):
        zeilen = (FIXTURES / "opencode_stream_success.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.ok is True
        assert r.error is None
        assert r.denials == []

    def test_tokens_werden_ueber_alle_schritte_summiert(self):
        """Die Aufnahme hat zwei step_finish: 4215+4397 ein, 74+6 aus."""
        zeilen = (FIXTURES / "opencode_stream_success.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.tokens_in == 8612
        assert r.tokens_out == 80

    def test_abgelehntes_werkzeug_wird_erkannt(self):
        zeilen = (FIXTURES / "opencode_stream_denied.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.ok is False
        assert len(r.denials) == 1
        assert "request.tools" in r.denials[0]["message"]

    def test_nicht_json_zeilen_kippen_den_lauf_nicht(self):
        zeilen = ["kein json", '{"type":"text","part":{"text":"hi"}}', ""]
        r = parse_events(zeilen)
        assert r.ok is True

    def test_leerer_strom_ist_kein_erfolg(self):
        r = parse_events([])
        assert r.ok is False
        assert "keine Ereignisse" in (r.error or "")
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py::TestEreignisse -q`
Erwartet: FAIL mit `ImportError: cannot import name 'parse_events'`

- [ ] **Schritt 3: Minimale Implementierung**

An `forge/runner_opencode.py` anhängen (Importe oben ergänzen):

```python
import json
import logging
from typing import Iterable

from forge.runner import RunResult

log = logging.getLogger(__name__)

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
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py -q`
Erwartet: PASS, 9 Tests

- [ ] **Schritt 5: Committen**

```bash
git add forge/runner_opencode.py tests/test_forge_runner_opencode.py tests/fixtures/opencode_stream_*.jsonl
git commit -m "feat(forge): opencode-Ereignisstrom auswerten

Gegen echte Aufnahmen. Tokens werden über alle step_finish summiert;
entfernte Werkzeuge werden am Marker 'was not in request.tools' erkannt."
```

---

### Task 4: opencode aufrufen

**Files:**
- Modify: `forge/runner_opencode.py`
- Test: `tests/test_forge_runner_opencode.py`

**Interfaces:**
- Consumes: `baue_config`, `parse_events`
- Produces: `run(prompt: str, cwd: Path, timeout: int, agent: str, model: str) -> RunResult`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_runner_opencode.py` anhängen:

```python
import json
import subprocess
from pathlib import Path

from forge import runner_opencode as ro


class TestAufruf:
    def _fake_run(self, aufzeichnung, stdout="", rc=0):
        def _run(cmd, **kwargs):
            aufzeichnung["cmd"] = cmd
            aufzeichnung["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, rc, stdout, "")
        return _run

    def test_kommando_setzt_agent_dir_und_format(self, monkeypatch, tmp_path):
        auf = {}
        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", self._fake_run(auf, '{"type":"text","part":{"text":"ok"}}'))
        ro.run("mach was", cwd=tmp_path, timeout=60, agent="spec", model="google/gemini-3.6-flash")
        assert "--agent" in auf["cmd"] and "spec" in auf["cmd"]
        assert "--format" in auf["cmd"] and "json" in auf["cmd"]
        assert "--dir" in auf["cmd"] and str(tmp_path) in auf["cmd"]

    def test_config_wird_ueber_umgebung_gesetzt_und_enthaelt_das_modell(self, monkeypatch, tmp_path):
        """Der Lauf darf nicht von ~/.config/opencode/opencode.json abhängen."""
        auf = {}
        inhalt = {}

        def _run(cmd, **kwargs):
            # Während des Laufs muss die Datei existieren — danach wird sie
            # entfernt, deshalb hier lesen und nicht hinterher.
            inhalt.update(json.loads(Path(kwargs["env"]["OPENCODE_CONFIG"]).read_text()))
            auf["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, '{"type":"text","part":{"text":"ok"}}', "")

        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _run)
        ro.run("x", cwd=tmp_path, timeout=60, agent="plan", model="nvidia/moonshotai/kimi-k3")

        assert "OPENCODE_CONFIG" in auf["kwargs"]["env"]
        assert inhalt["agent"]["plan"]["model"] == "nvidia/moonshotai/kimi-k3"
        assert inhalt["agent"]["plan"]["permission"]["bash"] == "deny"

    def test_temporaere_config_wird_hinterher_entfernt(self, monkeypatch, tmp_path):
        pfade = {}

        def _run(cmd, **kwargs):
            pfade["config"] = kwargs["env"]["OPENCODE_CONFIG"]
            return subprocess.CompletedProcess(cmd, 0, '{"type":"text","part":{"text":"ok"}}', "")

        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _run)
        ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert not Path(pfade["config"]).exists()

    def test_fehlendes_binary_ist_kein_absturz(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ro, "OPENCODE_BIN", None)
        r = ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert r.ok is False
        assert "opencode" in (r.error or "")

    def test_zeitueberschreitung_wird_als_fehler_gemeldet(self, monkeypatch, tmp_path):
        def _timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)
        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _timeout)
        r = ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert r.ok is False
        assert "Zeitüberschreitung" in (r.error or "")
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py::TestAufruf -q`
Erwartet: FAIL mit `AttributeError: module 'forge.runner_opencode' has no attribute 'OPENCODE_BIN'`

- [ ] **Schritt 3: Minimale Implementierung**

An `forge/runner_opencode.py` anhängen (Importe oben ergänzen):

```python
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Einmal beim Import aufgelöst, nicht je Lauf — dieselbe Begründung wie
# CLAUDE_BIN in forge/runner.py: ein launchd-Agent hat einen minimalen PATH,
# und ein fehlendes Binary soll als klare Meldung auftauchen, nicht als
# generischer OSError aus subprocess.
OPENCODE_BIN = shutil.which("opencode")


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

        return parse_events((ergebnis.stdout or "").splitlines())
    finally:
        # Die Config enthält keine Schlüssel, nur Verweise — trotzdem nicht
        # in /tmp liegen lassen.
        try:
            os.unlink(datei.name)
        except OSError:
            pass
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_opencode.py -q`
Erwartet: PASS, 14 Tests (4 Config, 5 Ereignisse, 5 Aufruf)

- [ ] **Schritt 5: Committen**

```bash
git add forge/runner_opencode.py tests/test_forge_runner_opencode.py
git commit -m "feat(forge): opencode-Läufe ausführen

Temporäre Config je Lauf über OPENCODE_CONFIG, damit ein unbeaufsichtigter
Lauf nicht von der globalen Benutzer-Config abhängt."
```

---

### Task 5: agy-Backend für die Review-Stufe

Der Reviewer bekommt **keine Werkzeuge**. `agy`s Rechtemodell ist ungemessen;
bis das nachgeholt ist, gibt es keinen Dateizugriff. Stattdessen bettet der
Runner den Diff aus `.forge/diff.patch` in den Prompt ein — die Datei schreibt
die Pipeline ohnehin schon vor der Review-Stufe (siehe `forge/stages.py`,
Kommentar zu `DIFF_DATEI`).

Und weil der Reviewer nichts schreiben kann, schreibt der Runner
`.forge/review.json` aus dessen Ausgabe.

**Files:**
- Create: `forge/runner_agy.py`
- Test: `tests/test_forge_runner_agy.py`

**Interfaces:**
- Consumes: `RunResult`, `forge.stages.DIFF_DATEI`, `forge.stages.VERDIKT_DATEI`
- Produces: `run(prompt: str, cwd: Path, timeout: int, agent: str, model: str) -> RunResult`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Tests für das agy-Backend (Antigravity, werkzeuglos)."""
import sys, os, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path

from forge import runner_agy as ra


def _fake(aufzeichnung, stdout, rc=0):
    def _run(cmd, **kwargs):
        aufzeichnung["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, rc, stdout, "")
    return _run


class TestDiffEinbettung:
    def test_diff_landet_im_prompt(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("--- a/x\n+++ b/x\n+neu\n")
        auf = {}
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(auf, '{"ok": true, "befunde": []}'))
        ra.run("Beurteile das.", cwd=tmp_path, timeout=60,
               agent="review", model="claude-opus-4-6-thinking")
        gesendet = " ".join(auf["cmd"])
        assert "+neu" in gesendet

    def test_fehlender_diff_ist_ein_fehler_kein_leeres_review(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "diff.patch" in (r.error or "")


class TestVerdikt:
    def test_runner_schreibt_die_verdikt_datei(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"ok": false, "befunde": ["zu lang"]}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        geschrieben = json.loads((tmp_path / ".forge" / "review.json").read_text())
        assert geschrieben["befunde"] == ["zu lang"]

    def test_json_im_codeblock_wird_erkannt(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run",
                            _fake({}, 'Hier mein Urteil:\n```json\n{"ok": true, "befunde": []}\n```\n'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        assert json.loads((tmp_path / ".forge" / "review.json").read_text())["ok"] is True

    def test_unparsebare_ausgabe_ist_ein_klarer_fehler(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, "Ich habe keine Meinung."))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "kein JSON" in (r.error or "")
        assert not (tmp_path / ".forge" / "review.json").exists()
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_agy.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'forge.runner_agy'`

- [ ] **Schritt 3: Minimale Implementierung**

```python
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
    """Führt einen werkzeuglosen agy-Lauf aus und schreibt das Verdikt."""
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
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_runner_agy.py -q`
Erwartet: PASS, 5 Tests

- [ ] **Schritt 5: Committen**

```bash
git add forge/runner_agy.py tests/test_forge_runner_agy.py
git commit -m "feat(forge): werkzeugloses agy-Backend für Review

Diff kommt aus .forge/diff.patch in den Prompt, das Verdikt schreibt der
Runner. agys Rechtemodell ist ungemessen, deshalb kein Dateizugriff."
```

---

### Task 6: Stufen auf Backends umstellen

**Files:**
- Modify: `forge/stages.py:76-83` (Stage-Dataclass), `forge/stages.py:160-186` (STAGES, FIX_STAGE)
- Modify: `forge/backends.py`
- Test: `tests/test_forge_stages.py`, `tests/test_forge_backends.py`

**Interfaces:**
- Consumes: `runner_opencode.run`, `runner_agy.run`
- Produces: `Stage.backend: str`, `Stage.model: str`,
  `backends.hole(name) -> Callable[..., RunResult]`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_stages.py` anhängen:

```python
from forge import backends
from forge.stages import ALLE_STUFEN, STAGES


class TestBackendZuordnung:
    def test_jede_stufe_hat_backend_und_modell(self):
        for stufe in ALLE_STUFEN:
            assert stufe.backend, f"{stufe.name} ohne Backend"
            assert stufe.model, f"{stufe.name} ohne Modell"

    def test_jedes_backend_ist_aufloesbar(self):
        for stufe in ALLE_STUFEN:
            assert callable(backends.hole(stufe.backend))

    def test_review_laeuft_auf_agy(self):
        review = next(s for s in STAGES if s.name == "review")
        assert review.backend == "agy"
        assert review.model == "claude-opus-4-6-thinking"

    def test_reviewer_ist_nicht_das_implementierer_modell(self):
        review = next(s for s in STAGES if s.name == "review")
        implement = next(s for s in STAGES if s.name == "implement")
        assert review.model != implement.model

    def test_unbekanntes_backend_wirft(self):
        import pytest
        with pytest.raises(KeyError, match="unbekanntes Backend"):
            backends.hole("gibtsnicht")
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_stages.py::TestBackendZuordnung -q`
Erwartet: FAIL mit `AttributeError: 'Stage' object has no attribute 'backend'`

- [ ] **Schritt 3: Implementierung**

An `forge/backends.py` anhängen:

```python
from typing import Callable


def hole(name: str) -> Callable:
    """Die run-Funktion eines Backends. Import erst hier, um Zyklen zu vermeiden
    (runner_agy importiert forge.stages, stages importiert forge.backends)."""
    from forge import runner_agy, runner_opencode
    tabelle = {
        "opencode": runner_opencode.run,
        "agy": runner_agy.run,
    }
    if name not in tabelle:
        raise KeyError(f"unbekanntes Backend: {name!r}")
    return tabelle[name]
```

In `forge/stages.py` die Dataclass um zwei Felder ergänzen:

```python
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
```

Und die Stufen um `backend`/`model` ergänzen — `STAGES` und `FIX_STAGE`
bekommen je einen zusätzlichen Schlüsselwortparameter:

```python
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
          backend="opencode", model="nvidia/minimaxai/minimax-m3"),
    Stage("review", m.REVIEWING, m.GATING,
          PermissionProfile(allowed=("Read", "Grep", "Glob", "Write"), mode="dontAsk"),
          _review_prompt, lambda t: VERDIKT_DATEI,
          backend="agy", model="claude-opus-4-6-thinking"),
)

FIX_STAGE = Stage(
    "fix", m.REVIEWING, m.GATING,
    PermissionProfile(allowed=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
                      mode="dontAsk"),
    _fix_prompt, lambda t: None, timeout=IMPLEMENT_TIMEOUT_SEKUNDEN,
    backend="opencode", model="nvidia/minimaxai/minimax-m3",
)
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_stages.py tests/test_forge_backends.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Committen**

```bash
git add forge/stages.py forge/backends.py tests/test_forge_stages.py tests/test_forge_backends.py
git commit -m "feat(forge): Stufen tragen Backend und Modell

Welche CLI eine Stufe ausführt, ist damit Daten statt Verzweigung.
Review läuft auf agy mit Opus 4.6 — anderer Anbieter als implement."
```

---

### Task 7: Pipeline über die Registry aufrufen

Die eine Naht. `forge/pipeline.py:439` ruft heute `runner.run` direkt.

**Files:**
- Modify: `forge/pipeline.py:439`
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `backends.hole`, `Stage.backend`, `Stage.model`
- Produces: keine neuen

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestBackendAufruf:
    def test_spec_stufe_laeuft_ueber_opencode(self, monkeypatch, stubs, tmp_path):
        """Die Pipeline darf runner.run nicht mehr fest verdrahtet aufrufen."""
        gesehen = {}

        def _fake_hole(name):
            def _run(prompt, cwd, timeout, agent, model):
                gesehen["backend"] = name
                gesehen["agent"] = agent
                gesehen["model"] = model
                return RunResult(ok=True, text="egal")
            return _run

        monkeypatch.setattr(pl.backends, "hole", _fake_hole)
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)

        assert gesehen["backend"] == "opencode"
        assert gesehen["agent"] == "spec"
        assert gesehen["model"] == "google/gemini-3.6-flash"

    def test_review_stufe_laeuft_ueber_agy(self, monkeypatch, stubs, tmp_path):
        gesehen = {}

        def _fake_hole(name):
            def _run(prompt, cwd, timeout, agent, model):
                gesehen["backend"] = name
                gesehen["model"] = model
                return RunResult(ok=True, text="egal")
            return _run

        monkeypatch.setattr(pl.backends, "hole", _fake_hole)
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

        assert gesehen["backend"] == "agy"
        assert gesehen["model"] == "claude-opus-4-6-thinking"
```

Der Aufruf ist `pl._eine_stufe_intern(task, task_id, state, worktree)` — die
Funktion, in der Zeile 439 steht. `_schreibe_diff` wird im Review-Test
ausgestubbt, weil die Pipeline `.forge/diff.patch` vor der Review-Stufe
schreibt und dafür sonst ein echtes git-Repo bräuchte. Die `stubs`-Fixture am
Kopf der Datei setzt `queue`, `journal` und `gitctl` bereits still.

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestBackendAufruf -q`
Erwartet: FAIL mit `AttributeError: module 'forge.pipeline' has no attribute 'backends'`

- [ ] **Schritt 3: Implementierung**

In `forge/pipeline.py` den Import ergänzen:

```python
from forge import backends
```

Und Zeile 439 ersetzen:

```python
    # Vorher: ergebnis = runner.run(prompt, cwd=worktree, profile=stufe.profile,
    #                               timeout=stufe.timeout)
    ergebnis = backends.hole(stufe.backend)(
        prompt, cwd=worktree, timeout=stufe.timeout,
        agent=stufe.name, model=stufe.model,
    )
```

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/ -q`
Erwartet: PASS bis auf die zwei vorbestehenden Fehlschläge in
`tests/test_api_auth.py`

- [ ] **Schritt 5: Committen**

```bash
git add forge/pipeline.py tests/test_forge_pipeline.py
git commit -m "feat(forge): Pipeline ruft Stufen über die Backend-Registry

Die eine Naht zwischen Pipeline und CLI. runner.py bleibt unverändert
als Claude-Code-Backend erhalten."
```

---

### Task 8: Ein echter Lauf gegen einen Wegwerf-Task

Kein Unit-Test, sondern die Abnahme: läuft eine Stufe wirklich durch.

**Files:**
- Keine Änderungen — nur Ausführung und Protokoll.

- [ ] **Schritt 1: Schlüssel prüfen**

```bash
source ~/.config/ai-keys.env && env | grep -c "NVIDIA_API_KEY\|GEMINI_API_KEY"
```
Erwartet: `2`

- [ ] **Schritt 2: Wegwerf-Worktree anlegen**

```bash
cd ~/Mantis && git worktree add /tmp/forge-abnahme -b forge-abnahme
mkdir -p /tmp/forge-abnahme/.forge
```

- [ ] **Schritt 3: Spec-Stufe von Hand auslösen**

```bash
cd ~/Mantis && source ~/.config/ai-keys.env && python3.14 -c "
from pathlib import Path
from forge import runner_opencode as ro
r = ro.run('Schreibe eine Datei hallo.md mit einem Satz. Antworte mit dem Pfad.',
           cwd=Path('/tmp/forge-abnahme'), timeout=600,
           agent='spec', model='google/gemini-3.6-flash')
print('ok:', r.ok, '| tokens:', r.tokens_in, r.tokens_out, '| error:', r.error)
print(r.text[:300])
"
```
Erwartet: `ok: True`, Tokenzahlen > 0, `hallo.md` existiert im Worktree.

- [ ] **Schritt 4: Review-Stufe gegen einen echten Diff auslösen**

```bash
cd /tmp/forge-abnahme && git add -A && git diff --cached > .forge/diff.patch
cd ~/Mantis && source ~/.config/ai-keys.env && python3.14 -c "
from pathlib import Path
from forge import runner_agy as ra
r = ra.run('Beurteile diese Änderung.', cwd=Path('/tmp/forge-abnahme'),
           timeout=600, agent='review', model='claude-opus-4-6-thinking')
print('ok:', r.ok, '| error:', r.error)
print(open('/tmp/forge-abnahme/.forge/review.json').read())
"
```
Erwartet: `ok: True`, `.forge/review.json` enthält gültiges JSON mit `ok` und
`befunde`.

- [ ] **Schritt 5: Aufräumen und Ergebnis festhalten**

```bash
cd ~/Mantis && git worktree remove --force /tmp/forge-abnahme && git branch -D forge-abnahme
```

Trage die gemessenen Laufzeiten und Tokenzahlen in
`tests/fixtures/permission_probe_opencode.md` unter einer neuen Überschrift
„Abnahmelauf" ein und committe:

```bash
git add tests/fixtures/permission_probe_opencode.md
git commit -m "docs: Abnahmelauf der Gratis-Backends festgehalten"
```

---

## Danach

Plan 2 (Budget, Ausweichketten, Parallelität, Nachtbetrieb, Merge-Freigabe),
Plan 3 (Scout und Telegram-Bot), Plan 4 (Dashboard). Jeder setzt auf dem
Ergebnis dieses Plans auf.
