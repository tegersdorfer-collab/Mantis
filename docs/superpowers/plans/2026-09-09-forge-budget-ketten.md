# Kontingent-Robustheit: Vorbedingungen, Budget und Ausweichketten — Implementierungsplan (Plan 2a)

> **Für agentische Worker:** ERFORDERLICHE SUB-SKILL: Nutze
> superpowers:subagent-driven-development (empfohlen) oder
> superpowers:executing-plans, um diesen Plan Task für Task umzusetzen.
> Die Schritte nutzen Checkbox-Syntax (`- [ ]`) zur Nachverfolgung.

**Ziel:** Die Forge übersteht erschöpfte Gratis-Kontingente, statt Tasks zu
verlieren — und die drei offenen Vorbedingungen aus Plan 1 sind geschlossen.

**Architektur:** Drei kleine Korrekturen an bestehendem Code, dann zwei neue
Module: `forge/budget.py` führt je Nacht und Anbieter Buch und merkt sich
Erschöpfung, `forge/ketten.py` wählt für eine Stufe das erste noch nutzbare
Modell. Die Pipeline fragt vor jedem Lauf die Kette statt das fest verdrahtete
`Stage.model`.

**Tech-Stack:** Python 3.14, PostgreSQL über `core.db`, pytest mit `monkeypatch`.
Keine neuen Abhängigkeiten.

**Spec:** [../specs/2026-09-09-forge-freetier-design.md](../specs/2026-09-09-forge-freetier-design.md)

## Global Constraints

- Python 3.14. Der Gate ruft `python3.14 -m pytest` (`forge/gate.py:157`).
- Keine neuen Pip-Abhängigkeiten.
- Kommentare, Docstrings und Testnamen auf Deutsch, wie im übrigen `forge/`.
- Tests laufen ohne Datenbank, ohne Netz und ohne echte CLI. `core.db` wird
  gemockt (Muster: bestehende forge-Tests), `subprocess.run` ebenso. Tests
  gegen echtes git sind erlaubt und für Task 3 verlangt — Muster:
  `TestC3GegenEchtesGit` in `tests/test_forge_pipeline.py`.
- **Niemals die echten CLIs `opencode` oder `agy` in einem Test aufrufen.**
  Deren Kontingent ist knapp und wird nur im Abnahme-Task verbraucht.
- Nach jedem Task: `python3.14 -m pytest tests/ -q` **vollständig grün** und
  `python3.14 -m ruff check .` sauber. Gemessene Grundlinie am 2026-09-09 nach
  dem Merge von Plan 1: **1361 passed, 0 failed**. Es gibt keine geduldeten
  Vorbestände.

## Eine Korrektur an der Spec

Die Spec sagt, das Budget solle bei Groq die echten Werte aus
`x-ratelimit-remaining-requests` und `-tokens` übernehmen. **Das ist nicht
umsetzbar:** die Forge ruft die `opencode`-CLI auf, nicht die HTTP-API, und die
CLI reicht keine Response-Header durch. Groq ist ohnehin nur `small_model` für
Sitzungstitel.

Konsequenz für dieses Design: Das Budget zählt für **alle** Anbieter lokal. Das
verlässliche Signal ist nicht die Vorhersage, sondern die Reaktion — die
Rate-Limit-Erkennung aus Plan 1 (`RunResult.rate_limited`). Die lokalen
Obergrenzen sind eine vorsorgliche Bremse davor, keine Buchhaltung mit
Anspruch auf Genauigkeit. Der Code muss das sagen, damit niemand die Zahlen
später für exakt hält.

## Dateistruktur

| Datei | Zuständigkeit |
|---|---|
| `forge/gate.py` (ändern) | `ERLAUBTE_ZONEN` + Prüfung in `pruefe` |
| `forge/pipeline.py` (ändern) | `git status`-Fehler weiterreichen; Rename-Quelle auslassen; Kette statt `Stage.model` |
| `core/db.py` (ändern) | Migrationen: `forge_budget`, `forge_tasks.implement_model` |
| `forge/budget.py` (neu) | Nacht-Schlüssel, Buchführung, Erschöpfung |
| `forge/ketten.py` (neu) | Ausweichketten je Stufe, Reviewer-≠-Implementierer-Regel |
| `forge/runner_opencode.py`, `forge/runner_agy.py` (ändern) | Erschöpfung ans Budget melden |
| `tests/test_forge_gate.py` (ändern) | Erlaubnisliste |
| `tests/test_forge_pipeline.py` (ändern) | Vorbedingungen 2+3, Kettenwahl |
| `tests/test_forge_budget.py` (neu) | Buchführung |
| `tests/test_forge_ketten.py` (neu) | Kettenwahl |

## Nicht in diesem Plan

Parallelität (zwei Bahnen), `AWAITING_APPROVAL`, Merge-Freigabe, Kurzbahn,
launchd-Nachtfenster und Mistral-Commit-Texte gehören zu **Plan 2b**. Scout und
Telegram-Bot zu Plan 3, Dashboard zu Plan 4.

`Stage.profile` bleibt weiterhin stehen (inert, erzwingt aber die
Claude-Code-Schranken bei der Konstruktion). Ein `claude`-Backend wird hier
nicht registriert.

---

### Task 1: Erlaubnisliste im Gate

Die Spec beschliesst erlaubte Pfade, das Gate prüft sie nirgends — es hat nur
die Verbotsliste `SPERRZONEN`. Damit trägt die Begründung nicht, mit der
`edit: allow` für alle Stufen akzeptiert wurde.

**Files:**
- Modify: `forge/gate.py:21-28` (nach `SPERRZONEN`), `forge/gate.py:188-221` (`pruefe`)
- Test: `tests/test_forge_gate.py`

**Interfaces:**
- Consumes: `_normalisiere_pfad(pfad: str) -> str` (bereits in `gate.py`)
- Produces: `ERLAUBTE_ZONEN: tuple[str, ...]`,
  `ausserhalb_erlaubter_zonen(dateien: list[str]) -> list[str]`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_gate.py` anhängen:

```python
from forge.gate import ERLAUBTE_ZONEN, ausserhalb_erlaubter_zonen


class TestErlaubteZonen:
    def test_erlaubte_pfade_gehen_durch(self):
        erlaubt = [
            "tests/test_neu.py",
            "docs/superpowers/specs/2026-01-01-x-design.md",
            "scripts/hilfe.sh",
            "tools/spotify/x.py",
            "core/skills/neu.py",
        ]
        assert ausserhalb_erlaubter_zonen(erlaubt) == []

    def test_kern_und_web_sind_draussen(self):
        assert ausserhalb_erlaubter_zonen(["core/db.py"]) == ["core/db.py"]
        assert ausserhalb_erlaubter_zonen(["web/api.py"]) == ["web/api.py"]

    def test_forge_darf_sich_nicht_selbst_umbauen(self):
        assert ausserhalb_erlaubter_zonen(["forge/pipeline.py"]) == ["forge/pipeline.py"]

    def test_core_skills_ist_die_ausnahme_in_core(self):
        gemischt = ["core/skills/gut.py", "core/agent.py"]
        assert ausserhalb_erlaubter_zonen(gemischt) == ["core/agent.py"]

    def test_praefix_matcht_nicht_ueber_die_verzeichnisgrenze(self):
        """'tests/' darf nicht 'testsuite.py' im Wurzelverzeichnis erlauben."""
        assert ausserhalb_erlaubter_zonen(["testsuite.py"]) == ["testsuite.py"]
        assert ausserhalb_erlaubter_zonen(["core/skillsammlung.py"]) == ["core/skillsammlung.py"]

    def test_fuehrendes_punktslash_wird_normalisiert(self):
        assert ausserhalb_erlaubter_zonen(["./tests/x.py"]) == []

    def test_wurzeldateien_sind_draussen(self):
        assert ausserhalb_erlaubter_zonen(["main.py", "README.md"]) == ["main.py", "README.md"]
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_gate.py::TestErlaubteZonen -q`
Erwartet: FAIL mit `ImportError: cannot import name 'ERLAUBTE_ZONEN' from 'forge.gate'`

- [ ] **Schritt 3: Implementierung**

In `forge/gate.py` direkt nach dem `SPERRZONEN`-Block einfügen:

```python
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
```

In `pruefe`, direkt nach dem `beruehrt_sperrzone`-Block:

```python
    draussen = ausserhalb_erlaubter_zonen(dateien)
    if draussen:
        gruende.append(f"ausserhalb der erlaubten Zonen: {', '.join(draussen)}")
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_gate.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`
Erwartet: grün und sauber. **Achtung:** Bestehende Gate-Tests konstruieren
Diffs mit Pfaden wie `forge/x.py`. Wenn einer davon jetzt aus einem neuen Grund
rot wird, ist das kein Defekt dieses Tasks, sondern ein Test, der eine
Annahme über erlaubte Pfade traf, die es vorher nicht gab — passe ihn an und
beschreibe die Anpassung im Report.

- [ ] **Schritt 6: Committen**

```bash
git add forge/gate.py tests/test_forge_gate.py
git commit -m "feat(forge): Erlaubnisliste im Gate

Die Spec beschliesst erlaubte Pfade, geprueft wurden sie nirgends. Ohne
diese Liste traegt die Begruendung nicht, mit der edit: allow fuer alle
Stufen akzeptiert wurde."
```

---

### Task 2: Fehlgeschlagenes `git status` parkt, statt zu schweigen

`_stufenarbeit_pfade` gibt bei nicht-null returncode `[]` zurück.
`_committe_stufenarbeit` liest das als "nichts zu committen" und meldet Erfolg —
die Stufe rückt mit uncommitteter Arbeit weiter. Genau der C3-Fehlermodus,
nur still. Zwei Funktionen weiter parkt `_schreibe_diff` bei demselben
Fehlerfall.

**Files:**
- Modify: `forge/pipeline.py:100-135` (`_stufenarbeit_pfade`),
  `forge/pipeline.py:138-160` (`_committe_stufenarbeit`),
  `forge/pipeline.py:212` (`_timeout_produkt`)
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `gitctl.run(*args, cwd) -> CompletedProcess`-ähnliches Objekt mit
  `returncode`, `stdout`, `stderr`
- Produces:
  `_stufenarbeit_pfade(worktree) -> tuple[list[str], list[str], str | None]`
  — `(zum_hinzufuegen, zum_committen, fehler)`. **Signaturänderung**: alle drei
  Aufrufstellen müssen mit.

**Warum zwei Listen statt einer.** `git add` und `git commit` brauchen
unterschiedliche Pfadmengen, sobald eine Umbenennung im Spiel ist — am
2026-09-10 gegen echtes git gemessen. In diesem Task sind beide Listen noch
identisch; Task 3 nutzt die Trennung. Die Aufteilung gehört hierher, damit
Task 3 nur noch Semantik ändert und nicht erneut die Signatur.

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestGitStatusFehlschlag:
    def _kaputtes_gitctl(self, monkeypatch):
        class _Ergebnis:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Ergebnis())

    def test_pfade_melden_den_fehler_statt_leerer_liste(self, monkeypatch, tmp_path):
        self._kaputtes_gitctl(monkeypatch)
        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert zum_hinzufuegen == []
        assert zum_committen == []
        assert fehler is not None
        assert "128" in fehler

    def test_beide_listen_sind_ohne_umbenennung_gleich(self, monkeypatch, tmp_path):
        class _Zwei:
            returncode = 0
            stdout = " M a.py\0?? b.py\0"
            stderr = ""

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Zwei())
        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert fehler is None
        assert zum_hinzufuegen == zum_committen == ["a.py", "b.py"]

    def test_committe_gibt_den_fehler_weiter_statt_erfolg(self, monkeypatch, tmp_path):
        """Der stille C3-Fehlermodus: Erfolg melden, obwohl nichts committet ist."""
        self._kaputtes_gitctl(monkeypatch)
        fehler = pl._committe_stufenarbeit(tmp_path, "implement", 1)
        assert fehler is not None
        assert "status" in fehler.lower()

    def test_leerer_worktree_bleibt_ein_erfolg(self, monkeypatch, tmp_path):
        """Nichts zu committen ist kein Fehler — nur ein Fehlschlag ist einer."""
        class _Leer:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(pl.gitctl, "run", lambda *a, **k: _Leer())
        assert pl._committe_stufenarbeit(tmp_path, "implement", 1) is None

    def test_timeout_produkt_meldet_kein_produkt_bei_kaputtem_status(self, monkeypatch, tmp_path):
        """Signatur ist _timeout_produkt(stufe, task, worktree, start)."""
        self._kaputtes_gitctl(monkeypatch)
        monkeypatch.setattr(pl, "_hat_neuen_commit", lambda w: False)
        implement = next(s for s in pl.stages.ALLE_STUFEN if s.name == "implement")
        hat_produkt, pfad = pl._timeout_produkt(implement, {"id": 1}, tmp_path, 0.0)
        assert hat_produkt is False
        assert pfad is None
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestGitStatusFehlschlag -q`
Erwartet: FAIL — `_stufenarbeit_pfade` gibt eine Liste zurück, kein Tupel, also
`TypeError: cannot unpack non-sequence` bzw. `ValueError: too many values to unpack`

- [ ] **Schritt 3: Implementierung**

In `_stufenarbeit_pfade` den Docstring um einen Absatz ergänzen und die
Rückgaben ändern:

```python
    Rückgabe ist (zum_hinzufuegen, zum_committen, fehler). Zwei Listen, weil
    `git add` und `git commit` bei Umbenennungen unterschiedliche Pfadmengen
    brauchen — hier sind sie noch identisch, Task 3 trennt sie inhaltlich.

    Ein fehlgeschlagenes `git status` darf NICHT als leere Liste zurückkommen:
    der Aufrufer läse das als "nichts zu committen" und liesse die Stufe mit
    uncommitteter Arbeit weiterrücken — genau der Fehlermodus, gegen den
    _committe_stufenarbeit gebaut wurde, nur still. `_schreibe_diff` behandelt
    denselben Fall ebenso als Fehler.
    """
    ergebnis = gitctl.run("status", "--porcelain", "-z", "--untracked-files=all", cwd=worktree)
    if ergebnis.returncode != 0:
        fehler = (f"git status fehlgeschlagen (returncode {ergebnis.returncode}): "
                  f"{(ergebnis.stderr or '').strip()[:300]}")
        log.warning(f"Forge-Pipeline: {fehler}")
        return [], [], fehler
```

und am Ende der Funktion:

```python
    geordnet = sorted(set(pfade))
    return geordnet, list(geordnet), None
```

In `_committe_stufenarbeit` die Aufrufstelle und die beiden git-Aufrufe:

```python
    zum_hinzufuegen, zum_committen, fehler = _stufenarbeit_pfade(worktree)
    if fehler is not None:
        return fehler
    if not zum_committen:
        return None
    hinzugefuegt = gitctl.run("add", "--", *zum_hinzufuegen, cwd=worktree)
```

und weiter unten der commit-Aufruf entsprechend mit `*zum_committen`.

In `_timeout_produkt` (Zeile 212). **Achtung:** das zweite Tupel-Element ist
ein Artefaktpfad für spec/plan, kein Fehlertext — dort gehört auch im
Fehlerfall `None` hin, nicht die Meldung:

```python
    if stufe.name in _ARBEITSSTUFEN:
        _, zum_committen, fehler = _stufenarbeit_pfade(worktree)
        if fehler is not None:
            # Kein Produkt nachweisbar, wenn git nicht antwortet. Der Fehler
            # wird eine Ebene höher beim Commit-Versuch erneut sichtbar.
            return False, None
        return _hat_neuen_commit(worktree) or bool(zum_committen), None
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py tests/test_forge_pipeline.py
git commit -m "fix(forge): fehlgeschlagenes git status parkt statt zu schweigen

_stufenarbeit_pfade gab bei Fehler eine leere Liste zurueck, was der
Aufrufer als 'nichts zu committen' las. Die Stufe rueckte dann mit
uncommitteter Arbeit weiter."
```

---

### Task 3: Gestagter Rename bricht `git add` nicht mehr ab

Steht nach einem fehlgeschlagenen Commit ein Rename im Index, matcht der
Quellpfad auf nichts mehr. `git add -- <pfade>` bricht bei **einem** schlechten
Pathspec komplett ab (returncode 128) — es wird gar nichts committet, der Task
parkt, und jeder Neuversuch scheitert erneut. Ohne manuelles `git reset` kommt
die Arbeit nie in einen Commit.

**Am 2026-09-10 gegen echtes git gemessen, das Ergebnis widerlegt die
naheliegende Lösung.** Nach `git mv alt.py neu.py` meldet
`git status --porcelain -z --untracked-files=all` genau `R  neu.py\0alt.py\0`.

- Quellpfad **auch** an `git add`: `fatal: pathspec 'alt.py' did not match any
  files`, returncode 128, und weil ein einziger schlechter Pathspec das ganze
  `add` abbricht, wird gar nichts committet.
- Quellpfad **weder** an `add` **noch** an `commit`: `git add -- neu.py` läuft,
  `git commit -- neu.py` läuft — aber danach steht `D  alt.py` weiterhin
  gestagt und uncommittet im Baum. Die Löschung der Quelle bleibt liegen, und
  der nächste Durchlauf scheitert erneut an genau diesem Pfad.
- Quellpfad **nur an `commit`**, nicht an `add`: `git status --porcelain` ist
  danach leer, `git show --stat` zeigt `alt.py => neu.py`. Das ist die richtige
  Form.

Deshalb die zwei Listen aus Task 2: `zum_hinzufuegen` ohne die Rename-Quelle,
`zum_committen` mit ihr.

**Files:**
- Modify: `forge/pipeline.py:100-135` (`_stufenarbeit_pfade`, Rename-Zweig)
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes:
  `_stufenarbeit_pfade(worktree) -> tuple[list[str], list[str], str | None]`
  aus Task 2
- Produces: keine neuen (nur geänderte Semantik der beiden Listen)

- [ ] **Schritt 1: Den fehlschlagenden Test gegen echtes git schreiben**

Dieser Test benutzt bewusst ein echtes Repository — der Fehler entsteht aus
gits Pathspec-Verhalten und ist gegen einen Stub nicht reproduzierbar. Muster:
die bestehende Klasse `TestC3GegenEchtesGit` in derselben Datei.

An `tests/test_forge_pipeline.py` anhängen:

```python
import subprocess


class TestGestagterRenameGegenEchtesGit:
    def _repo(self, tmp_path):
        def g(*args):
            return subprocess.run(["git", *args], cwd=tmp_path,
                                  capture_output=True, text=True)
        g("init", "-q")
        g("config", "user.email", "p@p")
        g("config", "user.name", "p")
        (tmp_path / "alt.py").write_text("inhalt\n")
        g("add", "-A")
        g("commit", "-qm", "start")
        return g

    def test_quelle_geht_an_commit_aber_nicht_an_add(self, tmp_path):
        """Gemessen am 2026-09-10: der Quellpfad eines gestagten Renames matcht
        bei `git add` auf nichts (returncode 128, bricht das ganze add ab),
        wird bei `git commit` aber gebraucht — sonst bleibt die Loeschung
        der Quelle gestagt und uncommittet liegen."""
        g = self._repo(tmp_path)
        g("mv", "alt.py", "neu.py")          # legt den Rename in den Index
        (tmp_path / "dazu.py").write_text("x\n")

        zum_hinzufuegen, zum_committen, fehler = pl._stufenarbeit_pfade(tmp_path)
        assert fehler is None
        assert "alt.py" not in zum_hinzufuegen, "Quelle gehoert nicht an git add"
        assert "alt.py" in zum_committen, "Quelle wird bei git commit gebraucht"
        assert "neu.py" in zum_hinzufuegen and "neu.py" in zum_committen
        assert "dazu.py" in zum_hinzufuegen and "dazu.py" in zum_committen

        # Der eigentliche Beweis: git add muss die Pfade annehmen.
        ergebnis = subprocess.run(["git", "add", "--", *zum_hinzufuegen], cwd=tmp_path,
                                  capture_output=True, text=True)
        assert ergebnis.returncode == 0, ergebnis.stderr

    def test_rename_landet_trotzdem_vollstaendig_im_commit(self, tmp_path):
        """Ohne den Quellpfad darf die Loeschung der Quelle nicht zurueckbleiben."""
        g = self._repo(tmp_path)
        g("mv", "alt.py", "neu.py")

        fehler = pl._committe_stufenarbeit(tmp_path, "implement", 1)
        assert fehler is None

        offen = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                               capture_output=True, text=True).stdout.strip()
        assert offen == "", f"nach dem Commit blieb offen: {offen!r}"
        vorhanden = subprocess.run(["git", "ls-files"], cwd=tmp_path,
                                   capture_output=True, text=True).stdout.split()
        assert "neu.py" in vorhanden
        assert "alt.py" not in vorhanden
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestGestagterRenameGegenEchtesGit -q`
Erwartet: FAIL bei `assert "alt.py" not in zum_hinzufuegen` — nach Task 2 sind
beide Listen noch identisch, die Quelle steht also auch in `zum_hinzufuegen`.

- [ ] **Schritt 3: Implementierung**

In `_stufenarbeit_pfade` zwei Listen führen statt einer. Die Sammelschleife:

```python
    nur_commit: list[str] = []
    pfade: list[str] = []
    i = 0
    while i < len(eintraege):
        eintrag = eintraege[i]
        i += 1
        # Format je Eintrag: zwei Statuszeichen, ein Leerzeichen, dann der Pfad.
        if len(eintrag) < 4:
            continue
        status, pfad = eintrag[:2], eintrag[3:]
        if status[0] in ("R", "C"):
            # Bei Umbenennung/Kopie folgt der Quellpfad als eigener Eintrag.
            # status[0] ist der INDEX-Status: 'R'/'C' dort heisst, der Rename
            # steht bereits im Index und die Quelle existiert im Arbeitsbaum
            # nicht mehr. Am 2026-09-10 gegen echtes git gemessen:
            #   - Quelle an `git add`  → returncode 128, und weil ein einziger
            #     schlechter Pathspec das ganze add abbricht, wird NICHTS
            #     committet; der Task haengt danach dauerhaft.
            #   - Quelle nirgends      → `D <quelle>` bleibt gestagt und
            #     uncommittet liegen, der naechste Durchlauf scheitert erneut.
            #   - Quelle nur an commit → sauber, `git show --stat` zeigt die
            #     Umbenennung, der Baum ist danach leer.
            if i < len(eintraege) and eintraege[i]:
                quelle = eintraege[i]
                i += 1
                if not _ist_intern(quelle):
                    nur_commit.append(quelle)
        if not _ist_intern(pfad):
            pfade.append(pfad)
    zum_hinzufuegen = sorted(set(pfade))
    zum_committen = sorted(set(pfade) | set(nur_commit))
    return zum_hinzufuegen, zum_committen, None
```

Der `return`-Zweig für den `git status`-Fehler aus Task 2 bleibt unverändert.

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py -q`
Erwartet: PASS, auch die bestehende `TestC3GegenEchtesGit`

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py tests/test_forge_pipeline.py
git commit -m "fix(forge): gestagter Rename bricht git add nicht mehr ab

Der Quellpfad eines bereits im Index stehenden Renames matcht auf nichts
mehr, und ein einziger schlechter Pathspec bricht das ganze add ab. Der
Task haengt danach dauerhaft und braucht ein manuelles git reset."
```

---

### Task 4: Migrationen für Budget und Implementierer-Modell

**Files:**
- Modify: `core/db.py:152-642` (ans Ende der `MIGRATIONS`-Liste)
- Test: `tests/test_forge_budget.py` (neu, nur der Schema-Vertrag)

**Interfaces:**
- Consumes: `MIGRATIONS: list[str]` in `core/db.py`
- Produces: Tabelle `forge_budget(nacht, provider, laeufe, tokens_in,
  tokens_out, erschoepft_seit, grund)` mit Primärschlüssel `(nacht, provider)`;
  Spalte `forge_tasks.implement_model TEXT`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Tests für forge/budget.py — Buchführung über die Gratis-Kontingente."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import db


class TestMigrationen:
    def _sql(self):
        return "\n".join(db.MIGRATIONS)

    def test_budget_tabelle_ist_migriert(self):
        sql = self._sql()
        assert "forge_budget" in sql
        for spalte in ("nacht", "provider", "laeufe", "tokens_in", "tokens_out",
                       "erschoepft_seit", "grund"):
            assert spalte in sql, f"Spalte {spalte} fehlt in der Migration"

    def test_primaerschluessel_ist_nacht_und_provider(self):
        assert "PRIMARY KEY (nacht, provider)" in self._sql()

    def test_implement_model_spalte_ist_migriert(self):
        assert "implement_model" in self._sql()

    def test_migrationen_sind_idempotent(self):
        """Jede Migration muss ein zweites Mal laufen können."""
        for m in db.MIGRATIONS:
            gross = m.upper()
            if gross.strip().startswith("CREATE TABLE"):
                assert "IF NOT EXISTS" in gross, m[:80]
            if gross.strip().startswith("ALTER TABLE") and "ADD COLUMN" in gross:
                assert "IF NOT EXISTS" in gross, m[:80]
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_budget.py -q`
Erwartet: FAIL mit `assert 'forge_budget' in sql`

- [ ] **Schritt 3: Implementierung**

Ans Ende der `MIGRATIONS`-Liste in `core/db.py`, vor der schliessenden `]`:

```python
    # forge_budget: je Nacht und Anbieter eine Zeile. Die Zahlen sind eine
    # vorsorgliche Bremse, KEINE genaue Buchhaltung — die Anbieter melden ihren
    # Reststand nicht, und die opencode-CLI reicht keine Response-Header durch.
    # Das verlässliche Signal ist erschoepft_seit, gesetzt aus einer echten
    # Rate-Limit-Antwort.
    """
    CREATE TABLE IF NOT EXISTS forge_budget (
        nacht          DATE NOT NULL,
        provider       TEXT NOT NULL,
        laeufe         INTEGER NOT NULL DEFAULT 0,
        tokens_in      BIGINT  NOT NULL DEFAULT 0,
        tokens_out     BIGINT  NOT NULL DEFAULT 0,
        erschoepft_seit TIMESTAMPTZ,
        grund          TEXT,
        PRIMARY KEY (nacht, provider)
    );
    """,

    # Welches Modell die implement/fix-Stufe zuletzt benutzt hat. Die
    # Review-Stufe liest das, um NICHT dasselbe Modell zu wählen: ein Modell,
    # das seinen eigenen Code abnimmt, sieht nur aus wie ein Review.
    "ALTER TABLE forge_tasks ADD COLUMN IF NOT EXISTS implement_model TEXT;",
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_budget.py -q`
Erwartet: PASS, 4 Tests

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add core/db.py tests/test_forge_budget.py
git commit -m "feat(db): Migrationen fuer forge_budget und implement_model"
```

---

### Task 5: `forge/budget.py` — Nachtschlüssel, Buchführung, Erschöpfung

**Files:**
- Create: `forge/budget.py`
- Test: `tests/test_forge_budget.py` (anhängen)

**Interfaces:**
- Consumes: `core.db.query`, `core.db.execute`, `core.db.query_one`
- Produces:
  - `provider_von_modell(model: str) -> str`
  - `nacht_id(jetzt: datetime | None = None) -> date`
  - `buche(model: str, tokens_in: int, tokens_out: int) -> None`
  - `markiere_erschoepft(model: str, grund: str) -> None`
  - `ist_erschoepft(model: str) -> bool`
  - `stand() -> list[dict]`
  - `OBERGRENZEN: dict[str, int]`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_budget.py` anhängen:

```python
from datetime import date, datetime

import pytest

from forge import budget


class TestProviderAusModell:
    def test_opencode_modelle_tragen_den_provider_vorn(self):
        assert budget.provider_von_modell("nvidia/moonshotai/kimi-k3") == "nvidia"
        assert budget.provider_von_modell("google/gemini-3.6-flash") == "google"
        assert budget.provider_von_modell("groq/openai/gpt-oss-120b") == "groq"

    def test_agy_modelle_haben_keinen_praefix(self):
        assert budget.provider_von_modell("claude-opus-4-6-thinking") == "antigravity"
        assert budget.provider_von_modell("gemini-3.1-pro-high") == "antigravity"


class TestNachtSchluessel:
    def test_abend_gehoert_zum_selben_tag(self):
        assert budget.nacht_id(datetime(2026, 9, 9, 23, 30)) == date(2026, 9, 9)

    def test_nach_mitternacht_gehoert_zur_vorherigen_nacht(self):
        """Ein Lauf um 02:00 gehört zur Nacht, die am Vorabend begann."""
        assert budget.nacht_id(datetime(2026, 9, 10, 2, 0)) == date(2026, 9, 9)

    def test_nachmittag_gehoert_zum_selben_tag(self):
        assert budget.nacht_id(datetime(2026, 9, 10, 13, 0)) == date(2026, 9, 10)


class TestBuchfuehrung:
    @pytest.fixture
    def db_stub(self, monkeypatch):
        zeilen = {}

        def _execute(sql, params=()):
            if "erschoepft_seit" in sql and "UPDATE" in sql.upper():
                zeilen[(params[-2], params[-1])] = dict(
                    zeilen.get((params[-2], params[-1]), {}), erschoepft_seit="jetzt", grund=params[0])
                return 1
            schluessel = (params[0], params[1])
            eintrag = zeilen.setdefault(
                schluessel, {"nacht": params[0], "provider": params[1],
                             "laeufe": 0, "tokens_in": 0, "tokens_out": 0,
                             "erschoepft_seit": None, "grund": None})
            eintrag["laeufe"] += 1
            eintrag["tokens_in"] += params[2]
            eintrag["tokens_out"] += params[3]
            return 1

        def _query_one(sql, params=()):
            return zeilen.get((params[0], params[1]))

        def _query(sql, params=()):
            return list(zeilen.values())

        monkeypatch.setattr(budget.db, "execute", _execute)
        monkeypatch.setattr(budget.db, "query_one", _query_one)
        monkeypatch.setattr(budget.db, "query", _query)
        return zeilen

    def test_buchen_summiert(self, db_stub):
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        budget.buche("nvidia/minimaxai/minimax-m3", 200, 20)
        eintrag = db_stub[(budget.nacht_id(), "nvidia")]
        assert eintrag["laeufe"] == 2
        assert eintrag["tokens_in"] == 300
        assert eintrag["tokens_out"] == 30

    def test_provider_werden_getrennt_gefuehrt(self, db_stub):
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        budget.buche("google/gemini-3.6-flash", 50, 5)
        assert db_stub[(budget.nacht_id(), "nvidia")]["tokens_in"] == 100
        assert db_stub[(budget.nacht_id(), "google")]["tokens_in"] == 50

    def test_frischer_provider_ist_nicht_erschoepft(self, db_stub):
        assert budget.ist_erschoepft("nvidia/moonshotai/kimi-k3") is False

    def test_markierte_erschoepfung_gilt(self, db_stub):
        budget.buche("claude-opus-4-6-thinking", 10, 1)
        budget.markiere_erschoepft("claude-opus-4-6-thinking", "Kontingent leer")
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is True

    def test_erschoepfung_gilt_fuer_den_ganzen_provider(self, db_stub):
        """Nicht nur für das Modell, das die Meldung ausgelöst hat."""
        budget.buche("nvidia/moonshotai/kimi-k3", 10, 1)
        budget.markiere_erschoepft("nvidia/moonshotai/kimi-k3", "leer")
        assert budget.ist_erschoepft("nvidia/minimaxai/minimax-m3") is True

    def test_obergrenze_erschoepft_vorsorglich(self, db_stub, monkeypatch):
        monkeypatch.setitem(budget.OBERGRENZEN, "antigravity", 2)
        budget.buche("claude-opus-4-6-thinking", 1, 1)
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is False
        budget.buche("claude-opus-4-6-thinking", 1, 1)
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is True

    def test_antigravity_grenze_liegt_unter_der_berichteten(self):
        """18 statt der berichteten 20 — die Zahl ist eine Community-Angabe."""
        assert budget.OBERGRENZEN["antigravity"] == 18
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_budget.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'forge.budget'`

- [ ] **Schritt 3: Implementierung**

```python
"""Buchführung über die Gratis-Kontingente, je Nacht und Anbieter.

**Was diese Zahlen sind und was nicht.** Die Anbieter melden ihren Reststand
nicht, und die Forge ruft die `opencode`-CLI auf statt der HTTP-API — Header
wie `x-ratelimit-remaining-*` kommen hier gar nicht an. Die Zähler sind
deshalb eine vorsorgliche Bremse, keine Buchhaltung mit Anspruch auf
Genauigkeit. Wer sie für exakt hält, plant falsch.

Das verlässliche Signal ist die Reaktion, nicht die Vorhersage: meldet ein
Anbieter ein Rate-Limit (RunResult.rate_limited aus forge/runner*.py), gilt er
für den Rest der Nacht als erschöpft. Die Obergrenzen unten greifen nur, wenn
diese Meldung ausbleibt.

Erschöpfung gilt immer für den ganzen Anbieter, nie nur für ein Modell: läuft
NVIDIAs Kontingent leer, hilft es nicht, dort ein anderes Modell zu probieren.
"""
import logging
from datetime import date, datetime, timedelta

from core import db

log = logging.getLogger(__name__)

# Stunde, vor der ein Lauf noch zur Nacht des Vortags gezählt wird. Ein Lauf um
# 02:00 gehört zur Nacht, die um 23:00 begann — sonst bekäme er um Mitternacht
# ein frisches Kontingent geschenkt, das es nicht gibt.
_NACHT_GRENZE_STUNDE = 12

# Läufe je Anbieter und Nacht, ab denen vorsorglich Schluss ist. Bewusst
# konservativ: für Antigravity sind ~20 Anfragen/Tag eine Community-Angabe,
# keine dokumentierte Zusage — 18 lässt Luft, statt den Account zu riskieren.
# NVIDIA und Google sind undokumentiert; die Zahlen sind Schätzungen und
# dürfen korrigiert werden, sobald jemand echte misst.
OBERGRENZEN = {
    "antigravity": 18,
    "nvidia": 120,
    "google": 200,
    "groq": 400,
}


def provider_von_modell(model: str) -> str:
    """Welcher Anbieter steckt hinter einer Modellkennung?

    opencode-Modelle tragen den Anbieter als erstes Pfadsegment
    ("nvidia/moonshotai/kimi-k3"). agy-Modelle haben kein solches Präfix
    ("claude-opus-4-6-thinking") und laufen alle über Antigravity.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    return "antigravity"


def nacht_id(jetzt: datetime | None = None) -> date:
    """Der Schlüssel der laufenden Nacht."""
    jetzt = jetzt or datetime.now()
    if jetzt.hour < _NACHT_GRENZE_STUNDE:
        return (jetzt - timedelta(days=1)).date()
    return jetzt.date()


def buche(model: str, tokens_in: int, tokens_out: int) -> None:
    """Zählt einen Lauf und seine Tokens auf den Anbieter des Modells."""
    provider = provider_von_modell(model)
    db.execute(
        "INSERT INTO forge_budget (nacht, provider, laeufe, tokens_in, tokens_out) "
        "VALUES (%s, %s, 1, %s, %s) "
        "ON CONFLICT (nacht, provider) DO UPDATE SET "
        "laeufe = forge_budget.laeufe + 1, "
        "tokens_in = forge_budget.tokens_in + EXCLUDED.tokens_in, "
        "tokens_out = forge_budget.tokens_out + EXCLUDED.tokens_out",
        (nacht_id(), provider, tokens_in, tokens_out),
    )


def markiere_erschoepft(model: str, grund: str) -> None:
    """Der Anbieter ist für den Rest der Nacht raus."""
    provider = provider_von_modell(model)
    log.warning(f"Forge-Budget: {provider} erschöpft — {grund}")
    db.execute(
        "UPDATE forge_budget SET erschoepft_seit = NOW(), grund = %s "
        "WHERE nacht = %s AND provider = %s",
        (grund, nacht_id(), provider),
    )


def ist_erschoepft(model: str) -> bool:
    """Darf dieses Modell heute Nacht noch benutzt werden?"""
    provider = provider_von_modell(model)
    zeile = db.query_one(
        "SELECT laeufe, erschoepft_seit FROM forge_budget WHERE nacht = %s AND provider = %s",
        (nacht_id(), provider),
    )
    if not zeile:
        return False
    if zeile.get("erschoepft_seit"):
        return True
    grenze = OBERGRENZEN.get(provider)
    return grenze is not None and (zeile.get("laeufe") or 0) >= grenze


def stand() -> list[dict]:
    """Alle Anbieter dieser Nacht — für Bericht und Dashboard."""
    return db.query(
        "SELECT * FROM forge_budget WHERE nacht = %s ORDER BY provider",
        (nacht_id(),),
    )
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_budget.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/budget.py tests/test_forge_budget.py
git commit -m "feat(forge): Budget-Buchfuehrung je Nacht und Anbieter

Erschoepfung gilt anbieterweit, nicht je Modell. Die Zaehler sind eine
vorsorgliche Bremse; das verlaessliche Signal ist die Rate-Limit-Meldung."
```

---

### Task 6: `forge/ketten.py` — Ausweichketten und die Reviewer-Regel

**Files:**
- Create: `forge/ketten.py`
- Test: `tests/test_forge_ketten.py`

**Interfaces:**
- Consumes: `forge.budget.ist_erschoepft(model) -> bool`
- Produces:
  - `KETTEN: dict[str, tuple[tuple[str, str], ...]]` — Stufenname auf Folge von
    `(backend, model)`
  - `waehle(stufe_name: str, verboten: frozenset[str] = frozenset()) -> tuple[str, str] | None`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Tests für die Ausweichketten der Forge-Stufen."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import ketten


@pytest.fixture
def leer(monkeypatch):
    """Nichts ist erschöpft, sofern der Test es nicht sagt."""
    erschoepft = set()
    monkeypatch.setattr(ketten.budget, "ist_erschoepft", lambda m: m in erschoepft)
    return erschoepft


class TestKettenAufbau:
    def test_jede_stufe_hat_eine_kette(self):
        for name in ("spec", "plan", "implement", "review", "fix"):
            assert ketten.KETTEN[name], f"Stufe {name} ohne Kette"

    def test_review_beginnt_auf_antigravity(self):
        backend, model = ketten.KETTEN["review"][0]
        assert backend == "agy"
        assert model == "claude-opus-4-6-thinking"

    def test_keine_kette_nennt_ein_modell_doppelt(self):
        for name, kette in ketten.KETTEN.items():
            modelle = [m for _, m in kette]
            assert len(modelle) == len(set(modelle)), f"{name} nennt ein Modell doppelt"


class TestWahl:
    def test_erstes_glied_wenn_nichts_erschoepft(self, leer):
        assert ketten.waehle("implement") == ketten.KETTEN["implement"][0]

    def test_weicht_auf_das_naechste_glied_aus(self, leer):
        leer.add(ketten.KETTEN["implement"][0][1])
        assert ketten.waehle("implement") == ketten.KETTEN["implement"][1]

    def test_leere_kette_gibt_none(self, leer):
        for _, model in ketten.KETTEN["implement"]:
            leer.add(model)
        assert ketten.waehle("implement") is None

    def test_unbekannte_stufe_gibt_none(self, leer):
        assert ketten.waehle("gibtsnicht") is None


class TestReviewerRegel:
    def test_verbotenes_modell_wird_uebersprungen(self, leer):
        erstes = ketten.KETTEN["review"][0][1]
        gewaehlt = ketten.waehle("review", verboten=frozenset({erstes}))
        assert gewaehlt is not None
        assert gewaehlt[1] != erstes

    def test_lieber_none_als_selbstabnahme(self, leer):
        """Bleibt nur noch das Implementierer-Modell übrig, wird nicht reviewt.
        Ein Modell, das seinen eigenen Code abnimmt, sieht nur aus wie ein Review."""
        kette = ketten.KETTEN["review"]
        letztes = kette[-1][1]
        for _, model in kette[:-1]:
            leer.add(model)
        assert ketten.waehle("review", verboten=frozenset({letztes})) is None
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_ketten.py -q`
Erwartet: FAIL mit `ModuleNotFoundError: No module named 'forge.ketten'`

- [ ] **Schritt 3: Implementierung**

```python
"""Welches Modell eine Stufe benutzt, wenn das bevorzugte nicht mehr kann.

Eine Kette ist geordnet: das erste Glied ist die erste Wahl. Ist sein Anbieter
erschöpft, rückt das nächste nach. Ist die Kette leer, liefert `waehle` None —
der Aufrufer parkt den Task dann, statt ihn scheitern zu lassen.

Alle Modelle sind am 2026-09-07 gegen die echten Anbieter geprüft worden;
DeepSeek V4 (Zeitüberschreitung), kimi-k2.6 und nemotron-ultra-253b (404 für
diesen Account) und gemma-4-31b (Zeitüberschreitung) stehen deshalb NICHT
hier drin, obwohl Kataloge sie führen.
"""
from forge import budget

# Stufenname → geordnete Folge von (backend, model).
KETTEN: dict[str, tuple[tuple[str, str], ...]] = {
    "spec": (
        ("opencode", "google/gemini-3.6-flash"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "nvidia/minimaxai/minimax-m3"),
    ),
    "plan": (
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
        ("opencode", "nvidia/minimaxai/minimax-m3"),
    ),
    # Die letzten Glieder von implement und fix liegen bewusst bei einem
    # ANDEREN Anbieter. Erschöpfung gilt anbieterweit — eine Kette aus lauter
    # NVIDIA-Modellen wäre mit einer einzigen Rate-Limit-Meldung komplett tot,
    # und zwar bei der Stufe, ohne die kein Task fertig wird. Gemini Flash ist
    # für Code schwächer als MiniMax; ein schwächeres Modell, dessen Arbeit das
    # Gate prüft und Timo freigibt, ist besser als gar keine Stufe.
    "implement": (
        ("opencode", "nvidia/minimaxai/minimax-m3"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "fix": (
        ("opencode", "nvidia/minimaxai/minimax-m3"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "review": (
        ("agy", "claude-opus-4-6-thinking"),
        ("agy", "gemini-3.1-pro-high"),
        ("opencode", "nvidia/moonshotai/kimi-k3"),
    ),
}


def waehle(stufe_name: str, verboten: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """Das erste nutzbare Glied der Kette, oder None.

    `verboten` trägt die Reviewer-Regel: die Review-Stufe bekommt hier das
    Modell hinein, mit dem implementiert wurde. Bleibt danach nichts übrig,
    ist None die richtige Antwort — der Task wird geparkt statt von dem
    Modell abgenommen, das ihn geschrieben hat.
    """
    for backend, model in KETTEN.get(stufe_name, ()):
        if model in verboten:
            continue
        if budget.ist_erschoepft(model):
            continue
        return backend, model
    return None
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_ketten.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/ketten.py tests/test_forge_ketten.py
git commit -m "feat(forge): Ausweichketten je Stufe

Nur am 2026-09-07 gegen die echten Anbieter gepruefte Modelle. Die
Reviewer-Regel steckt in waehle(): lieber None als Selbstabnahme."
```

---

### Task 7: Erschöpfung ans Budget melden

Beide Backends setzen seit Plan 1 `RunResult.rate_limited`, aber niemand bucht
das. Ebenso werden Tokens nirgends gezählt.

**Files:**
- Modify: `forge/pipeline.py` (nach dem Backend-Aufruf in `_eine_stufe_intern`)
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `forge.budget.buche(model, tokens_in, tokens_out)`,
  `forge.budget.markiere_erschoepft(model, grund)`,
  `RunResult.rate_limited`, `.tokens_in`, `.tokens_out`, `.error`
- Produces: keine neuen

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestBudgetMeldung:
    def _budget_stub(self, monkeypatch):
        gebucht, erschoepft = [], []
        monkeypatch.setattr(pl.budget, "buche",
                            lambda m, ti, to: gebucht.append((m, ti, to)))
        monkeypatch.setattr(pl.budget, "markiere_erschoepft",
                            lambda m, g: erschoepft.append((m, g)))
        return gebucht, erschoepft

    def test_erfolgreicher_lauf_wird_gebucht(self, monkeypatch, stubs, tmp_path):
        gebucht, _ = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=True, text="egal", tokens_in=1200, tokens_out=80)))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert gebucht and gebucht[-1][1:] == (1200, 80)

    def test_rate_limit_markiert_den_anbieter_als_erschoepft(self, monkeypatch, stubs, tmp_path):
        _, erschoepft = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="rate limit", rate_limited=True)))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert erschoepft, "Rate-Limit wurde nicht ans Budget gemeldet"

    def test_gewoehnlicher_fehler_erschoepft_nichts(self, monkeypatch, stubs, tmp_path):
        _, erschoepft = self._budget_stub(monkeypatch)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(
                ok=False, error="irgendwas anderes")))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert erschoepft == []
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestBudgetMeldung -q`
Erwartet: FAIL mit `AttributeError: module 'forge.pipeline' has no attribute 'budget'`

- [ ] **Schritt 3: Implementierung**

Import in `forge/pipeline.py` ergänzen (die bestehende Sammelzeile):

```python
from forge import backends, budget, gate, gitctl, journal, models as m, queue, runner, stages
```

Direkt nach dem Backend-Aufruf in `_eine_stufe_intern`:

```python
    # Verbrauch buchen, bevor irgendein Zweig zurückspringt — auch ein
    # gescheiterter Lauf hat Kontingent gekostet.
    budget.buche(stufe.model, ergebnis.tokens_in, ergebnis.tokens_out)
    if ergebnis.rate_limited:
        budget.markiere_erschoepft(stufe.model, (ergebnis.error or "Rate-Limit")[:200])
```

`stufe.model` ist hier noch das fest verdrahtete Modell aus Plan 1. Task 8
ersetzt beide Vorkommen durch das aus der Kette gewählte — bis dahin bucht die
Pipeline auf das Modell, das sie tatsächlich benutzt, und das ist richtig.

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py tests/test_forge_pipeline.py
git commit -m "feat(forge): Verbrauch und Erschoepfung ans Budget melden"
```

---

### Task 8: Die Pipeline wählt über die Kette

**Files:**
- Modify: `forge/pipeline.py` (`_eine_stufe_intern`, Backend-Aufruf)
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `forge.ketten.waehle(stufe_name, verboten) -> tuple[str, str] | None`,
  `queue.setze_artefakt` (Muster für Task-Feld-Updates)
- Produces: keine neuen

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestKettenwahl:
    def test_stufe_laeuft_mit_dem_glied_aus_der_kette(self, monkeypatch, stubs, tmp_path):
        gesehen = {}
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", "test/modell-x"))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: (
                gesehen.update(backend=name, model=model) or RunResult(ok=True, text="egal"))))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert gesehen["backend"] == "opencode"
        assert gesehen["model"] == "test/modell-x"

    def test_leere_kette_parkt_statt_zu_scheitern(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "geparkt"
        assert stubs["parks"], "kein Park-Eintrag geschrieben"

    def test_review_bekommt_das_implementierer_modell_als_verboten(self, monkeypatch, stubs, tmp_path):
        gesehen = {}

        def _waehle(name, verboten=frozenset()):
            gesehen[name] = verboten
            return ("agy", "claude-opus-4-6-thinking")

        monkeypatch.setattr(pl.ketten, "waehle", _waehle)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        pl._eine_stufe_intern({"id": 1, "implement_model": "nvidia/minimaxai/minimax-m3"},
                              1, m.REVIEWING, tmp_path)
        assert "nvidia/minimaxai/minimax-m3" in gesehen["review"]

    def test_implement_merkt_sich_sein_modell_am_task(self, monkeypatch, stubs, tmp_path):
        gemerkt = []
        monkeypatch.setattr(pl.queue, "merke_implement_modell",
                            lambda task_id, model: gemerkt.append((task_id, model)))
        monkeypatch.setattr(pl.ketten, "waehle",
                            lambda name, verboten=frozenset(): ("opencode", "test/impl"))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
        pl._eine_stufe_intern({"id": 1}, 1, m.IMPLEMENTING, tmp_path)
        assert gemerkt == [(1, "test/impl")]
```

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestKettenwahl -q`
Erwartet: FAIL mit `AttributeError: module 'forge.pipeline' has no attribute 'ketten'`

- [ ] **Schritt 3: Implementierung**

Import ergänzen:

```python
from forge import backends, budget, gate, gitctl, journal, ketten, models as m, queue, runner, stages
```

In `forge/queue.py` ergänzen (Muster: `setze_artefakt`):

```python
def merke_implement_modell(task_id: int, model: str) -> None:
    """Womit implementiert wurde — die Review-Stufe darf nicht dasselbe nehmen."""
    db.execute("UPDATE forge_tasks SET implement_model=%s WHERE id=%s", (model, task_id))
```

In `_eine_stufe_intern`, den Backend-Aufruf ersetzen:

```python
    # Die Review-Stufe darf nicht auf dem Modell laufen, das implementiert hat.
    verboten = frozenset()
    if stufe.name == "review":
        vorher = task.get("implement_model")
        if vorher:
            verboten = frozenset({vorher})

    wahl = ketten.waehle(stufe.name, verboten=verboten)
    if wahl is None:
        _park(task_id, state,
              f"Kette für Stufe '{stufe.name}' erschöpft — kein nutzbares Modell "
              f"übrig (Task {task_id})")
        return "geparkt"
    backend_name, modell = wahl

    ergebnis = backends.hole(backend_name)(
        prompt, cwd=worktree, timeout=stufe.timeout,
        agent=stufe.name, model=modell,
    )

    budget.buche(modell, ergebnis.tokens_in, ergebnis.tokens_out)
    if ergebnis.rate_limited:
        budget.markiere_erschoepft(modell, (ergebnis.error or "Rate-Limit")[:200])

    if stufe.name in ("implement", "fix"):
        queue.merke_implement_modell(task_id, modell)
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py -q`
Erwartet: PASS. **Achtung:** `TestBackendAufruf` aus Plan 1 prüft
`stufe.backend`/`stufe.model`; diese Tests werden jetzt von der Kette
überstimmt. Passe sie an, sodass sie die Kettenwahl prüfen, und beschreibe
die Anpassung im Report — eine Zusicherung, die nichts mehr zusichert, ist
schlechter als eine ehrlich ersetzte.

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py forge/queue.py tests/test_forge_pipeline.py
git commit -m "feat(forge): Stufen waehlen ihr Modell ueber die Ausweichkette

Erschoepfte Anbieter werden uebersprungen, eine leere Kette parkt den Task.
Review bekommt das Implementierer-Modell als verboten mit."
```

---

### Task 9: Abnahmelauf gegen erschöpfte Ketten

Kein Unit-Test, sondern der Beweis, dass die Erschöpfungslogik im Zusammenspiel
funktioniert. **Ohne echte CLI-Aufrufe** — die Erschöpfung wird über das Budget
herbeigeführt, nicht über echtes Leerlaufen.

**Files:** keine Änderungen, nur Ausführung und Protokoll.

- [ ] **Schritt 1: Datenbank erreichbar?**

```bash
cd ~/Mantis && python3.14 -c "
from core import db
db.init_pool(); db.run_migrations()
print('Migrationen gelaufen')
print(db.query('SELECT * FROM forge_budget LIMIT 1'))
"
```
Erwartet: „Migrationen gelaufen" und eine leere Liste. Schlägt das fehl, ist
Postgres nicht erreichbar — melde BLOCKED, ändere nichts.

- [ ] **Schritt 2: Buchführung gegen die echte Tabelle**

```bash
cd ~/Mantis && python3.14 -c "
from core import db
from forge import budget
db.init_pool()
budget.buche('nvidia/moonshotai/kimi-k3', 1000, 50)
budget.buche('nvidia/minimaxai/minimax-m3', 500, 25)
print('Stand:', budget.stand())
print('erschoepft?', budget.ist_erschoepft('nvidia/moonshotai/kimi-k3'))
"
```
Erwartet: eine Zeile für `nvidia` mit `laeufe=2`, `tokens_in=1500`,
`tokens_out=75`; `erschoepft? False`.

- [ ] **Schritt 3: Erschöpfung und Ausweichen**

```bash
cd ~/Mantis && python3.14 -c "
from core import db
from forge import budget, ketten
db.init_pool()
print('vorher :', ketten.waehle('implement'))
budget.markiere_erschoepft('nvidia/minimaxai/minimax-m3', 'Abnahmelauf')
print('nachher:', ketten.waehle('implement'))
print('review mit verbotenem Modell:',
      ketten.waehle('review', verboten=frozenset({'claude-opus-4-6-thinking'})))
"
```
Erwartet: `vorher` nennt ein NVIDIA-Modell. `nachher` darf **kein** weiteres
NVIDIA-Modell nennen, weil Erschöpfung anbieterweit gilt — richtig ist
`('opencode', 'google/gemini-3.6-flash')`, das anbieterfremde letzte Glied.
`review` weicht auf `('agy', 'gemini-3.1-pro-high')` aus.

Zwei Beobachtungen wären Defekte und gehören in den Report: `nachher` nennt
noch ein NVIDIA-Modell (dann gilt Erschöpfung nur je Modell statt je
Anbieter), oder `nachher` ist `None` (dann greift das anbieterfremde Glied
nicht).

- [ ] **Schritt 4: Aufräumen**

```bash
cd ~/Mantis && python3.14 -c "
from core import db
from forge import budget
db.init_pool()
db.execute('DELETE FROM forge_budget WHERE nacht = %s', (budget.nacht_id(),))
print('Testzeilen entfernt:', budget.stand())
"
```
Erwartet: leere Liste.

- [ ] **Schritt 5: Ergebnis festhalten und committen**

Trage die beobachteten Ausgaben in
`tests/fixtures/permission_probe_opencode.md` unter einer neuen Überschrift
„Abnahmelauf Budget und Ketten" ein und committe:

```bash
git add tests/fixtures/permission_probe_opencode.md
git commit -m "docs: Abnahmelauf Budget und Ketten festgehalten"
```

---

## Danach

Plan 2b: Parallelität (zwei Bahnen mit `FOR UPDATE SKIP LOCKED`),
`AWAITING_APPROVAL`, Merge-Freigabe, Kurzbahn und das launchd-Nachtfenster.
Danach Plan 3 (Scout und Telegram-Bot) und Plan 4 (Dashboard).
