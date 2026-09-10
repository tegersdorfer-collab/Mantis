# Die Nacht übersteht Fehler — Implementierungsplan (Plan 2b)

> **Für agentische Worker:** ERFORDERLICHE SUB-SKILL: Nutze
> superpowers:subagent-driven-development (empfohlen) oder
> superpowers:executing-plans, um diesen Plan Task für Task umzusetzen.
> Die Schritte nutzen Checkbox-Syntax (`- [ ]`) zur Nachverfolgung.

**Ziel:** Die beiden Zustände, aus denen die Forge sich heute nicht mehr
befreien kann, sind behoben — leere Kontingente und ein negatives Review.

**Architektur:** Zwei kleine, präzise Änderungen an bestehendem Verhalten, und
danach der Integrationstest, dessen Fehlen beide Fehler durchgelassen hat. Ein
neuer Tick-Ausgang `"kontingent"` trennt „Anbieter leer" von „etwas ist kaputt",
damit Erschöpfung nicht mehr die Fehler-Spirale füttert. Und die Review-Stufe
rückt bei negativem Urteil nicht mehr vor, wodurch die bereits vollständig
gebaute Fix-Schleife zum ersten Mal erreichbar wird.

**Tech-Stack:** Python 3.14, PostgreSQL über `core.db`, pytest mit `monkeypatch`.
Keine neuen Abhängigkeiten.

**Spec:** [../specs/2026-09-09-forge-freetier-design.md](../specs/2026-09-09-forge-freetier-design.md)
— Abschnitt „Offene Vorbedingungen für Plan 2b".

## Global Constraints

- Python 3.14. Der Gate ruft `python3.14 -m pytest` (`forge/gate.py`).
- Keine neuen Pip-Abhängigkeiten.
- Kommentare, Docstrings und Testnamen auf Deutsch, wie im übrigen `forge/`.
- Unit-Tests laufen ohne Datenbank, ohne Netz und ohne echte CLI.
  **Task 3 ist die begründete Ausnahme** und läuft gegen die echte Datenbank,
  mit `skipif`, wenn sie nicht erreichbar ist.
- **Niemals `opencode` oder `agy` aufrufen.** Deren Kontingent ist knapp.
- Nach jedem Task: `python3.14 -m pytest tests/ -q` **vollständig grün** und
  `python3.14 -m ruff check .` sauber. Gemessene Grundlinie am 2026-09-10 nach
  dem Merge von Plan 2a: **1432 passed, 0 failed**.

## Warum dieser Plan so klein ist

Plan 2a wurde nach neun Tasks vom Abschluss-Review als nicht merge-reif
befunden — dreimal, weil jeder Unit-Test `subprocess` und die Datenbank mockt
und Integrationsfehler deshalb still durchgehen. Dieselbe Ursache steckt hinter
beiden Fehlern, die dieser Plan behebt: die Fix-Schleife ist vollständig
implementiert und getestet, aber **kein Test hat je geprüft, ob sie erreichbar
ist.**

Deshalb sind hier nur drei Tasks, und der dritte ist der eigentliche Ertrag.
Parallelität, `AWAITING_APPROVAL`, Merge-Freigabe, Kurzbahn und das
launchd-Nachtfenster kommen in **Plan 2c**, auf dieser Grundlage.

## Dateistruktur

| Datei | Zuständigkeit |
|---|---|
| `forge/pipeline.py` (ändern) | Erschöpfung liefert `"kontingent"`; Review bleibt bei negativem Urteil stehen |
| `forge/daemon.py` (ändern) | `"kontingent"` schläft, ohne die Fehler-Spirale zu füttern |
| `tests/test_forge_pipeline.py` (ändern) | beide Verhaltensänderungen |
| `tests/test_forge_daemon.py` (ändern) | der neue Ausgang in `main` |
| `tests/test_forge_lebenszyklus.py` (neu) | Ende-zu-Ende gegen echte Zustandsmaschine und echte DB |

## Nicht in diesem Plan

Parallelität (zwei Bahnen), `AWAITING_APPROVAL`, Merge-Freigabe, Kurzbahn,
launchd-Nachtfenster, Mistral-Commit-Texte — alle in Plan 2c. Scout und
Telegram-Bot in Plan 3, Dashboard in Plan 4.

Ebenfalls nicht hier: dass `implement_model` nur den letzten Schreiber merkt.
Das wird erst mit einer erreichbaren Fix-Schleife real und gehört in denselben
Plan wie die Mehrfach-Runden — 2c.

---

### Task 1: `"kontingent"` als eigener Tick-Ausgang

Heute liefert eine erschöpfte Kette `"fehler"`. `daemon.main` zählt das in
`failures`, und nach `MAX_CONSECUTIVE_FAILURES` schreibt es
`~/.mantis-forge-stop`. `should_run` verweigert danach jede Arbeit, über
Neustarts hinweg, bis die Datei von Hand gelöscht wird. **Eine Nacht mit leeren
Kontingenten legt die Forge für alle folgenden Nächte still.**

Leere Anbieter sind kein Fehlverhalten. Sie brauchen einen eigenen Ausgang.

**Files:**
- Modify: `forge/pipeline.py` (`_eine_stufe_intern`, der Erschöpfungszweig)
- Modify: `forge/daemon.py` (`main`, die Sleep-Kette)
- Test: `tests/test_forge_pipeline.py`, `tests/test_forge_daemon.py`

**Interfaces:**
- Consumes: `ketten.kette_erschoepft(stufe_name) -> bool`
- Produces: neuer Rückgabewert `"kontingent"` aus `pipeline.eine_stufe` und
  `daemon.tick`; neue Konstante `daemon.KONTINGENT_SLEEP_SECONDS`

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestKontingentAusgang:
    def test_erschoepfte_kette_liefert_kontingent_nicht_fehler(self, monkeypatch, stubs, tmp_path):
        """'fehler' würde in die Fehler-Spirale zählen und die Forge abschalten."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "kontingent"

    def test_reviewer_kollision_bleibt_geparkt(self, monkeypatch, stubs, tmp_path):
        """Nur Erschöpfung ist Kontingent. Eine Kollision ist weiterhin ein Park."""
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: False)
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        assert ergebnis == "geparkt"
```

An `tests/test_forge_daemon.py` anhängen:

```python
class TestKontingentImDaemon:
    def test_kontingent_zaehlt_nicht_in_die_fehler_spirale(self, monkeypatch):
        """Drei Kontingent-Ticks dürfen den Not-Aus NICHT auslösen."""
        gesehen = {"schlaefe": [], "stop_geschrieben": False}
        ticks = iter(["kontingent"] * 5)

        class _Halt(BaseException):
            pass

        def _tick():
            try:
                return next(ticks)
            except StopIteration:
                raise _Halt

        def _sleep(s):
            gesehen["schlaefe"].append(s)

        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", _sleep)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.STOP_FILE, "exists", lambda: False)
        monkeypatch.setattr(
            d.STOP_FILE, "write_text",
            lambda *a, **k: gesehen.__setitem__("stop_geschrieben", True))

        try:
            d.main()
        except _Halt:
            pass

        assert gesehen["stop_geschrieben"] is False, "Not-Aus trotz reinem Kontingent-Grund"
        assert gesehen["schlaefe"], "Kontingent-Tick hat gar nicht geschlafen"
        assert all(s == d.KONTINGENT_SLEEP_SECONDS for s in gesehen["schlaefe"])

    def test_fehler_loest_den_not_aus_weiterhin_aus(self, monkeypatch):
        """Die Spirale bleibt für echte Fehler erhalten."""
        gesehen = {"stop_geschrieben": False}
        ticks = iter(["fehler"] * 5)

        class _Halt(BaseException):
            pass

        def _tick():
            try:
                return next(ticks)
            except StopIteration:
                raise _Halt

        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", lambda s: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.STOP_FILE, "exists", lambda: False)
        monkeypatch.setattr(
            d.STOP_FILE, "write_text",
            lambda *a, **k: gesehen.__setitem__("stop_geschrieben", True))

        try:
            d.main()
        except _Halt:
            pass

        assert gesehen["stop_geschrieben"] is True
```

`tests/test_forge_daemon.py` importiert bereits `from forge import daemon as d`
und `from forge import models as m` — das Präfix `d` oben stimmt also. Die
`_Halt`-Klasse erbt bewusst von `BaseException`, damit sie nicht von einem
`except Exception` im Daemon geschluckt wird.

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestKontingentAusgang tests/test_forge_daemon.py::TestKontingentImDaemon -q`
Erwartet: FAIL — `assert 'fehler' == 'kontingent'` und
`AttributeError: module 'forge.daemon' has no attribute 'KONTINGENT_SLEEP_SECONDS'`

- [ ] **Schritt 3: Implementierung**

In `forge/pipeline.py`, im Erschöpfungszweig von `_eine_stufe_intern`, den
Rückgabewert von `"fehler"` auf `"kontingent"` ändern und den Kommentar
ergänzen:

```python
            # Kontingent, nicht Fehlverhalten: Zustand bewusst unverändert
            # lassen, damit der Task weiterläuft, sobald die Anbieter wieder da
            # sind. Spec Zeile 264: "Erst wenn jede Kette trocken ist, endet
            # die Nacht."
            #
            # Eigener Rückgabewert statt "fehler": daemon.main zählt "fehler"
            # in die Fehler-Spirale, und drei in Folge schreiben die
            # Not-Aus-Datei ~/.mantis-forge-stop, die jeden Neustart überlebt.
            # Eine Nacht mit leeren Kontingenten hätte die Forge damit für alle
            # folgenden Nächte stillgelegt.
            return "kontingent"
```

In `forge/daemon.py` bei den anderen Sleep-Konstanten:

```python
# Wartezeit, wenn alle Anbieter für diese Nacht leer sind. Deutlich länger als
# die anderen Bremsen: vor dem nächsten Kontingent-Fenster ändert sich nichts,
# und jeder Tick bis dahin ist eine Datenbankabfrage ohne Ergebnis.
KONTINGENT_SLEEP_SECONDS = 900
```

Und in `main`, in der Sleep-Kette:

```python
        elif ergebnis == "kontingent":
            time.sleep(KONTINGENT_SLEEP_SECONDS)
```

Wichtig: `failures = failures + 1 if ergebnis == "fehler" else 0` bleibt
unverändert — `"kontingent"` setzt den Zähler damit sogar zurück, was richtig
ist. Ein leerer Anbieter ist kein Beleg dafür, dass etwas kaputt ist.

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py tests/test_forge_daemon.py -q`
Erwartet: PASS

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py forge/daemon.py tests/test_forge_pipeline.py tests/test_forge_daemon.py
git commit -m "fix(forge): leere Kontingente schalten die Forge nicht mehr ab

Erschoepfung lieferte 'fehler', der Daemon zaehlte das in die
Fehler-Spirale, und drei in Folge schrieben die Not-Aus-Datei — die jeden
Neustart ueberlebt. Eine Nacht mit leeren Kontingenten hat die Forge damit
fuer alle folgenden Naechte stillgelegt.

Eigener Ausgang 'kontingent': schlaeft lang, zaehlt nicht als Fehler."
```

---

### Task 2: Ein negatives Review erreicht die Fix-Stufe

Die Fix-Schleife ist vollständig gebaut. `_waehle_stufe` wählt `FIX_STAGE`,
sobald `REVIEWING` mit einem negativen Urteil angetroffen wird; `FIX_STAGE`
läuft in `REVIEWING` und rückt danach nach `IMPLEMENTING` vor
(`ziel = m.IMPLEMENTING if ist_fix else stufe.next_state`);
`_verwirf_review_artefakte` räumt das veraltete Urteil weg;
`MAX_FIXRUNDEN` begrenzt die Runden.

Nichts davon läuft je. Die Review-Stufe rückt **auch bei negativem Urteil**
nach `GATING` vor. Der Zustand steht danach nie wieder auf `REVIEWING`, also
wählt `_waehle_stufe` nie `FIX_STAGE`. Das Gate liest das negative Urteil, und
`daemon._gate_und_abschliessen` parkt den Task. **Das erste negative Review
parkt dauerhaft.**

**Files:**
- Modify: `forge/pipeline.py` (`_eine_stufe_intern`, kurz vor `ziel = …`)
- Test: `tests/test_forge_pipeline.py`

**Interfaces:**
- Consumes: `_hat_negatives_verdikt(worktree) -> bool` (bereits vorhanden),
  `stages.FIX_STAGE`
- Produces: keine neuen

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

An `tests/test_forge_pipeline.py` anhängen:

```python
class TestFixStufeErreichbar:
    def _review_mit_verdikt(self, monkeypatch, tmp_path, ok: bool):
        """Lässt die Review-Stufe laufen und legt das angegebene Urteil ab."""
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text(
            json.dumps({"verdict": "pass" if ok else "fail",
                        "findings": [] if ok else [{"severity": "critical", "what": "x"}]}))
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
        return pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

    def test_negatives_verdikt_bleibt_in_reviewing(self, monkeypatch, stubs, tmp_path):
        """Sonst wählt _waehle_stufe nie die Fix-Stufe."""
        self._review_mit_verdikt(monkeypatch, tmp_path, ok=False)
        ziele = [z for z in stubs["states"]]
        assert m.GATING not in [z[1] for z in ziele], \
            "Review ist trotz negativem Urteil nach GATING vorgerueckt"

    def test_positives_verdikt_geht_weiter_nach_gating(self, monkeypatch, stubs, tmp_path):
        self._review_mit_verdikt(monkeypatch, tmp_path, ok=True)
        assert m.GATING in [z[1] for z in stubs["states"]]

    def test_naechster_durchlauf_waehlt_die_fix_stufe(self, tmp_path):
        """Der Beweis, dass die Schleife wirklich geschlossen ist."""
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text(
            json.dumps({"verdict": "fail",
                        "findings": [{"severity": "critical", "what": "x"}]}))
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is True
        assert stufe.name == "fix"
```

Prüfe, ob `json` in der Testdatei bereits importiert ist, und ergänze es sonst.

Zur `stubs`-Fixture: sie zeichnet Zustandswechsel als `(task_id, ziel)` auf
(`lambda tid, target, current: aufz["states"].append((tid, target))`) — **nicht**
als `(von, nach)`. `z[1]` ist damit der Zielzustand, was die Assertions oben
korrekt macht. Parks liegen analog als `(task_id, grund)` in `stubs["parks"]`.

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestFixStufeErreichbar -q`
Erwartet: FAIL bei `test_negatives_verdikt_bleibt_in_reviewing` — die Review-Stufe
rückt heute unbedingt nach `GATING` vor. `test_naechster_durchlauf_waehlt_die_fix_stufe`
sollte bereits grün sein: `_waehle_stufe` ist korrekt, sie wird nur nie in
diesem Zustand aufgerufen. Ein bereits grüner Test ist hier kein Fehler,
sondern genau der Befund — schreib das in den Report.

- [ ] **Schritt 3: Implementierung**

In `_eine_stufe_intern`, unmittelbar vor der Zeile
`ziel = m.IMPLEMENTING if ist_fix else stufe.next_state`:

```python
    # Ein negatives Review rückt NICHT vor. Die Fix-Schleife ist vollständig
    # gebaut (_waehle_stufe → FIX_STAGE, FIX_STAGE → IMPLEMENTING,
    # _verwirf_review_artefakte, MAX_FIXRUNDEN), aber sie war nie erreichbar:
    # die Review-Stufe ging auch mit negativem Urteil nach GATING, der Zustand
    # stand danach nie wieder auf REVIEWING, und das Gate parkte den Task.
    # Das erste negative Review war damit ein Sackgassen-Park.
    #
    # Ein Selbstübergang REVIEWING → REVIEWING ist in forge/models.py bewusst
    # NICHT erlaubt, deshalb wird hier gar kein Zustandswechsel versucht: der
    # Task bleibt schlicht stehen, und der nächste Durchlauf trifft ihn in
    # REVIEWING mit vorliegendem negativem Urteil an — genau die Bedingung,
    # auf die _waehle_stufe wartet.
    if stufe.name == "review" and _hat_negatives_verdikt(worktree):
        journal.log(task_id, "stage_done",
                    f"Review negativ für Task {task_id} — Fix-Runde folgt")
        return "weiter"

    ziel = m.IMPLEMENTING if ist_fix else stufe.next_state
```

- [ ] **Schritt 4: Test laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py -q`
Erwartet: PASS. Bestehende Tests, die von „Review geht immer nach GATING"
ausgingen, werden jetzt fehlschlagen — das ist der Befund, nicht ein Defekt.
Passe sie so an, dass sie die tatsächliche Eigenschaft prüfen, und beschreibe
jede Anpassung im Report.

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/pipeline.py tests/test_forge_pipeline.py
git commit -m "fix(forge): ein negatives Review erreicht die Fix-Stufe

Die Fix-Schleife war vollstaendig gebaut und getestet, aber nie
erreichbar: Review rueckte auch mit negativem Urteil nach GATING vor, der
Zustand stand danach nie wieder auf REVIEWING, und das Gate parkte den
Task. Das erste negative Review war ein Sackgassen-Park.

Review rueckt jetzt nur mit positivem Urteil vor."
```

---

### Task 3: Der Lebenszyklus-Test gegen die echte Zustandsmaschine

Der eigentliche Ertrag dieses Plans. Beide oben behobenen Fehler haben neun
bzw. acht grüne Task-Reviews überlebt, weil jeder Unit-Test die Datenbank und
`subprocess` mockt: **kein Test hat je einen Task durch mehr als eine Stufe
geschickt.** Dieser Test tut das.

Er läuft gegen die echte Datenbank, weil die Zustandsübergänge dort
stattfinden — `queue.set_state` ist ein Compare-and-Swap in SQL, und ein
handgeschriebener Stub davon wäre genau die Art Attrappe, die in Plan 2a
bereits einmal einen Fehler verdeckt hat. Fehlt die Datenbank, wird der Test
übersprungen, nicht gefälscht.

Die LLM-Backends bleiben gemockt: sie kosten Kontingent und sind nicht das,
was hier geprüft wird.

**Files:**
- Create: `tests/test_forge_lebenszyklus.py`

**Interfaces:**
- Consumes: `core.db`, `forge.queue`, `forge.pipeline`, `forge.models`,
  `forge.stages`, `forge.backends`
- Produces: keine

- [ ] **Schritt 1: Den fehlschlagenden Test schreiben**

```python
"""Ende-zu-Ende: ein Task durchläuft mehrere Stufen gegen die ECHTE
Zustandsmaschine und die ECHTE Datenbank.

Warum echt und nicht gemockt: Beide Fehler, die Plan 2b behebt, haben je
acht bis neun grüne Task-Reviews überlebt, weil jeder Unit-Test die Datenbank
mockt und deshalb keiner einen Task durch mehr als eine Stufe geschickt hat.
`queue.set_state` ist ein Compare-and-Swap in SQL; ein handgeschriebener Stub
davon wäre genau die Attrappe, die in Plan 2a schon einmal einen Fehler
verdeckt hat.

Die LLM-Backends bleiben gemockt — sie kosten knappes Kontingent und sind
nicht der Prüfgegenstand.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import db
from forge import backends, models as m, pipeline as pl, queue, stages
from forge.runner import RunResult


def _db_erreichbar() -> bool:
    try:
        db.init_pool()
        db.query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_erreichbar(), reason="PostgreSQL nicht erreichbar — Lebenszyklus-Test übersprungen")


@pytest.fixture
def task_id():
    """Ein echter Task in der echten Tabelle, hinterher restlos entfernt."""
    neue_id = queue.enqueue("Lebenszyklus-Test", "wegwerf", source="test", priority=1)
    yield neue_id
    # forge_journal.task_id trägt ON DELETE CASCADE (core/db.py), die
    # Journaleinträge verschwinden also mit dem Task.
    db.execute("DELETE FROM forge_tasks WHERE id=%s", (neue_id,))


@pytest.fixture
def gemockte_backends(monkeypatch):
    """Jede Stufe gelingt; die Rückgabe ist inhaltlich leer."""
    monkeypatch.setattr(backends, "hole", lambda name: (
        lambda prompt, cwd, timeout, agent, model: RunResult(ok=True, text="egal")))
    monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
    monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
    monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
    monkeypatch.setattr(pl, "_committe_artefakt", lambda *a: None)


def _zustand(task_id: int) -> str:
    return db.query_one("SELECT state FROM forge_tasks WHERE id=%s", (task_id,))["state"]


class TestLebenszyklus:
    def test_task_laeuft_von_queued_bis_gating(self, task_id, gemockte_backends, tmp_path):
        """Der Weg, den es geben muss: vier Stufen, kein Park."""
        gesehen = [_zustand(task_id)]
        for _ in range(6):
            zustand = _zustand(task_id)
            if zustand == m.GATING:
                break
            task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
            ergebnis = pl._eine_stufe_intern(task, task_id, zustand, tmp_path)
            assert ergebnis != "geparkt", f"geparkt in {zustand}"
            gesehen.append(_zustand(task_id))

        assert _zustand(task_id) == m.GATING, f"Verlauf: {gesehen}"

    def test_negatives_review_landet_in_der_fix_runde(self, task_id, gemockte_backends, tmp_path):
        """Der Beweis, dass die Fix-Schleife erreichbar ist — der Fehler aus Task 2."""
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        queue.set_state(task_id, m.PLANNING, current=m.SPECCING)
        queue.set_state(task_id, m.IMPLEMENTING, current=m.PLANNING)
        queue.set_state(task_id, m.REVIEWING, current=m.IMPLEMENTING)

        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text(
            json.dumps({"verdict": "fail",
                        "findings": [{"severity": "critical", "what": "x"}]}))

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        pl._eine_stufe_intern(task, task_id, m.REVIEWING, tmp_path)
        assert _zustand(task_id) == m.REVIEWING, "Review ist trotz negativem Urteil vorgerueckt"

        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is True and stufe.name == "fix"

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        pl._eine_stufe_intern(task, task_id, m.REVIEWING, tmp_path)
        assert _zustand(task_id) == m.IMPLEMENTING, "Fix-Stufe hat nicht nach IMPLEMENTING gefuehrt"

    def test_erschoepfte_kette_laesst_den_zustand_stehen(self, task_id, monkeypatch, tmp_path):
        """Kontingent darf keinen Zustand verbrennen — der Fehler aus Task 1."""
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)

        task = db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))
        ergebnis = pl._eine_stufe_intern(task, task_id, m.SPECCING, tmp_path)

        assert ergebnis == "kontingent"
        assert _zustand(task_id) == m.SPECCING, "Zustand wurde trotz Kontingent veraendert"
```

Beides ist vorab geprüft: `queue.enqueue(title, description="", source="timo",
priority=50) -> int` — der Aufruf oben passt. Und `forge_journal.task_id` hat
`REFERENCES forge_tasks(id) ON DELETE CASCADE`, weshalb die Fixture nur den
Task löscht.

- [ ] **Schritt 2: Test laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_lebenszyklus.py -q`

Erwartet, **wenn du Task 1 und 2 noch nicht umgesetzt hast**: zwei Fehlschläge,
einer je behobenem Fehler. Wenn Task 1 und 2 bereits stehen, ist dieser Test
grün — dann weise seine Aussagekraft nach, indem du beide Fixes einzeln
zurückdrehst und die zugehörigen Fehlschläge im Report festhältst. Ein Test,
der bei zurückgedrehtem Fix grün bleibt, ist wertlos und muss verschärft
werden.

- [ ] **Schritt 3: Aufräumen belegen**

```bash
python3.14 -c "
from core import db
db.init_pool()
print('Testtasks uebrig:', db.query(\"SELECT count(*) AS n FROM forge_tasks WHERE title='Lebenszyklus-Test'\")[0]['n'])
"
```
Erwartet: `0`. Bleibt etwas übrig, ist die Fixture kaputt — beheben, nicht von
Hand aufräumen.

- [ ] **Schritt 4: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`
Erwartet: grün, und die Gesamtzahl ist um die neuen Tests gestiegen. Läuft
keine Datenbank, erscheinen sie als `skipped` — auch das ist ein gültiger Lauf,
muss aber im Report stehen.

- [ ] **Schritt 5: Committen**

```bash
git add tests/test_forge_lebenszyklus.py
git commit -m "test(forge): Lebenszyklus gegen echte Zustandsmaschine und echte DB

Beide Fehler aus Plan 2b haben acht bis neun gruene Task-Reviews
ueberlebt, weil jeder Unit-Test die Datenbank mockt und deshalb kein Test
einen Task je durch mehr als eine Stufe geschickt hat. Dieser tut das,
gegen echtes Postgres, mit skipif wenn es fehlt."
```

---

## Danach

Plan 2c: Parallelität (zwei Bahnen mit `FOR UPDATE SKIP LOCKED`),
`AWAITING_APPROVAL`, Merge-Freigabe, Kurzbahn, Mistral-Commit-Texte und das
launchd-Nachtfenster. Danach Plan 3 (Scout und Telegram-Bot) und Plan 4
(Dashboard).

Mit Plan 2b ist die Vorbedingung erfüllt, unter der ein Nachtlauf überhaupt
sinnvoll ist: die Forge kommt aus beiden Sackgassen wieder heraus.
