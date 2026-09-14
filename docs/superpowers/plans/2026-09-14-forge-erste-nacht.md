# Die erste Nacht — Implementierungsplan (Plan 2c)

> **Für agentische Worker:** ERFORDERLICHE SUB-SKILL: Nutze
> superpowers:subagent-driven-development (empfohlen) oder
> superpowers:executing-plans, um diesen Plan Task für Task umzusetzen.
> Die Schritte nutzen Checkbox-Syntax (`- [ ]`) zur Nachverfolgung.

**Ziel:** Die Forge kann eine Nacht mit einer Bahn unbeaufsichtigt laufen —
mit geschlossener Fix-Schleife, Freigabe am Morgen, Zeitfenster und einem
Simulator, der die Nacht vor der Nacht durchspielt.

**Architektur:** Sechs Tasks, in Abhängigkeitsreihenfolge. Erst die
Test-Isolation (`source='test'`), auf der alle DB-Tests dieses Plans stehen.
Dann die drei Verhaltensänderungen an Pipeline, Zustandsmaschine und Daemon —
jede klein, jede mit Mutationsnachweis. Dann das CLI, mit dem Timo morgens
freigibt. Zuletzt der Nachtlauf-Simulator, der das echte `daemon.main()` gegen
die echte Datenbank treibt und die vier Importants aus dem 2b-Review
rückwirkend rot machen muss.

**Tech-Stack:** Python 3.14, PostgreSQL über `core.db`, pytest mit `monkeypatch`,
argparse, launchd. Keine neuen Abhängigkeiten.

**Spec:** [../specs/2026-09-09-forge-freetier-design.md](../specs/2026-09-09-forge-freetier-design.md)
— Abschnitt **„Nachtrag 2c (2026-09-14)"**. Wo der Nachtrag der Spec darüber
widerspricht, gilt der Nachtrag.

## Global Constraints

- Python 3.14. Das Gate ruft `python3.14 -m pytest` (`forge/gate.py`).
- Keine neuen Pip-Abhängigkeiten.
- Kommentare, Docstrings, Testnamen und Commit-Nachrichten auf Deutsch, wie im
  übrigen `forge/`.
- Unit-Tests laufen ohne Datenbank, ohne Netz und ohne echte CLI. Ausnahmen mit
  `skipif`, wenn Postgres fehlt: `tests/test_forge_lebenszyklus.py` und
  `tests/test_forge_nachtlauf.py`. Diese beiden legen Zeilen **nur** mit
  `source='test'` an und räumen sie am Fixture-Anfang und -Ende weg.
- **Niemals `opencode` oder `agy` aufrufen.** Deren Kontingent ist knapp.
- Nach jedem Task: `python3.14 -m pytest tests/ -q` **vollständig grün** und
  `python3.14 -m ruff check .` sauber. Grundlinie am 2026-09-14 nach dem Merge
  von Plan 2b: **1446 passed, 0 failed**.
- **Mutationsnachweis** ist Teil jedes Tasks, der Verhalten ändert (Tasks 2, 3,
  4, 6): Fix zurückdrehen, benannten Test rot sehen, Ausgabe in den Report.
- **Review-Regel aus dem Nachtrag:** Jedes Task-Review-Briefing stellt die fünf
  Systemfragen (wer führt das noch aus; was tut der nächste Tick; was, wenn der
  Prozess dazwischen stirbt; was überlebt einen Neustart; was landet in
  Produktionstabellen). Der Reviewer beantwortet jede mit einer Codestelle.

## Dateistruktur

| Datei | Zuständigkeit |
|---|---|
| `forge/queue.py` (ändern) | Quellenfilter in `active`/`claim_next`; `fixrunden`, `hole`, `nach_zustand`, `requeue` |
| `forge/pipeline.py` (ändern) | Fix bleibt in REVIEWING; Runde zählt nach Erfolg; Journal-Kind `kontingent` |
| `forge/models.py` (ändern) | `AWAITING_APPROVAL` statt `AWAITING_RESTART`; `REVIEWING → IMPLEMENTING` entfällt |
| `forge/journal.py` (ändern) | Kind `kontingent` |
| `forge/daemon.py` (ändern) | Gate → `AWAITING_APPROVAL`; Nachtfenster; weicher Stop |
| `forge/launchd/com.mantis.forge.plist` (neu) | Kalenderstart 23:00, kein KeepAlive |
| `forge/freigabe.py` (neu) | `freigeben`, `ablehnen`, `neu_einreihen` — die Freigabe-Logik hinter CLI und (später) Bot |
| `forge/bericht.py` (neu) | `morgenbericht()` — der Text, den Plan 3 per Telegram schickt |
| `forge/cli.py` (neu) | `python3.14 -m forge.cli status\|approve\|reject\|requeue\|stop` |
| `tests/test_forge_queue.py` (ändern) | Quellenfilter, neue Queue-Funktionen |
| `tests/test_forge_pipeline.py` (ändern) | Fix-Schleife, Rundenzählung, Journal-Kind |
| `tests/test_forge_models.py` (ändern) | neue Übergänge |
| `tests/test_forge_daemon.py` (ändern) | Gate-Ausgang, Nachtfenster, Halt |
| `tests/test_forge_lebenszyklus.py` (ändern) | Fixture auf Quellenfilter; Fix-Tick bleibt in REVIEWING |
| `tests/test_forge_freigabe.py` (neu) | Freigabe gegen gemocktes und echtes git |
| `tests/test_forge_bericht.py` (neu) | Morgenbericht |
| `tests/test_forge_cli.py` (neu) | Subkommandos |
| `tests/test_forge_nachtlauf.py` (neu) | der Simulator |

## Nicht in diesem Plan

Parallelität (zwei Bahnen, `worker`-Spalte), Kurzbahn, Mistral-Commit-Texte —
Plan 2d. Telegram-Bot und Scout — Plan 3. Dashboard — Plan 4.

---

### Task 1: Test-Isolation über `source='test'`

Heute schützt `tests/test_forge_lebenszyklus.py` seinen Testtask mit einer
zehnjährigen Pause. Das hält, ist aber ein Trick, und der Simulator (Task 6)
braucht das Gegenteil: `daemon.tick()` soll den Testtask **claimen**. Eine
Regel für beides: Zeilen mit `source='test'` sind für den Daemon unsichtbar,
und Tests sehen ausschliesslich ihre eigenen.

**Files:**
- Modify: `forge/queue.py` (`active`, `claim_next`)
- Modify: `tests/test_forge_queue.py`
- Modify: `tests/test_forge_lebenszyklus.py` (Fixture `task_id`, Klasse
  `TestTaskFixturePausiertSichSelbst`)

**Interfaces:**
- Produces: `queue.TEST_QUELLE = "test"`;
  `queue.active(quelle: str | None = None) -> dict | None`;
  `queue.claim_next(quelle: str | None = None) -> dict | None`.
  `quelle=None` ist Produktion (alles ausser `TEST_QUELLE`), ein String ist
  genau diese Quelle.

- [ ] **Schritt 1: Die fehlschlagenden Tests schreiben**

An `tests/test_forge_queue.py` anhängen (die Helfer `_patch` und `q` existieren
dort bereits):

```python
class TestQuellenTrennung:
    """Nachtrag 2c: Testzeilen sind für den Daemon unsichtbar, und Tests sehen
    nur ihre eigenen. Beides über EINE Regel in der SQL, nicht über eine Pause."""

    def test_produktion_schliesst_die_testquelle_aus(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active()
        sql, params = rec.queries[-1]
        assert "source <> %s" in sql
        assert params[-1] == q.TEST_QUELLE

    def test_testlauf_sieht_nur_seine_quelle(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.active(quelle="test")
        sql, params = rec.queries[-1]
        assert "source = %s" in sql
        assert params[-1] == "test"

    def test_claim_next_traegt_die_quelle_in_beide_abfragen(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.claim_next(quelle="test")
        assert len(rec.queries) == 2, "active() und die Queue-Abfrage"
        assert all("source = %s" in sql and params[-1] == "test"
                   for sql, params in rec.queries)

    def test_claim_next_ohne_quelle_ist_produktion(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.claim_next()
        assert all("source <> %s" in sql and params[-1] == q.TEST_QUELLE
                   for sql, params in rec.queries)
```

In `tests/test_forge_lebenszyklus.py` die Fixture `task_id` ersetzen (den
Docstring aus Plan 2b durch den neuen ersetzen, `queue.pause`-Aufruf und die
Importe `datetime`/`timedelta` entfernen, falls sonst ungenutzt):

```python
@pytest.fixture
def task_id():
    """Ein echter Task in der echten Tabelle, hinterher restlos entfernt.

    Schutz gegen die Produktion (Nachtrag 2c): die Zeile trägt
    `source='test'`, und `queue.active()`/`queue.claim_next()` filtern diese
    Quelle in der Produktion aus. Ein Daemon, der parallel tickt, sieht den
    Task nie — auch dann nicht, wenn pytest per SIGKILL endet (das Gate der
    Forge ruft pytest mit `timeout=1800`) und die Teardown-Zeile nie läuft.
    Der Sweep am Anfang räumt Leichen aus genau solchen Läufen weg.
    """
    db.execute("DELETE FROM forge_tasks WHERE source=%s", (queue.TEST_QUELLE,))
    neue_id = queue.enqueue("Lebenszyklus-Test", "wegwerf", source=queue.TEST_QUELLE, priority=1)
    yield neue_id
    # forge_journal.task_id trägt ON DELETE CASCADE (core/db.py), die
    # Journaleinträge verschwinden also mit dem Task.
    db.execute("DELETE FROM forge_tasks WHERE id=%s", (neue_id,))
```

Die Klasse `TestTaskFixturePausiertSichSelbst` durch diese ersetzen:

```python
class TestQuellenTrennungGegenEchteDb:
    def test_produktion_sieht_den_testtask_nicht_einmal_aktiv(self, task_id):
        """Der Beweis, auf dem alle DB-Tests dieses Plans stehen."""
        queue.set_state(task_id, m.SPECCING, current=m.QUEUED)
        gesehen = queue.active()
        assert gesehen is None or gesehen["id"] != task_id, \
            "Produktions-active() hat den Testtask geliefert"
        assert queue.active(quelle=queue.TEST_QUELLE)["id"] == task_id

    def test_produktion_claimt_den_testtask_nicht(self, task_id):
        gesehen = queue.claim_next()
        assert gesehen is None or gesehen["id"] != task_id, \
            "Produktions-claim_next() hat den Testtask geclaimt"
        assert _zustand(task_id) == m.QUEUED, "claim_next hat den Testtask trotzdem bewegt"
```

Achtung beim zweiten Test: `queue.claim_next()` ohne Quelle ist ein echter
Produktions-Claim. Liegt ein echter `queued`-Task in der Tabelle, würde der
Test ihn nach `speccing` setzen. Am 2026-09-14 gibt es keinen (4 Tasks, alle
`parked`), aber der Test darf sich darauf nicht verlassen: vor dem Aufruf mit
`db.query_one("SELECT count(*) AS n FROM forge_tasks WHERE state=%s AND source<>%s", (m.QUEUED, queue.TEST_QUELLE))["n"]`
prüfen und mit `pytest.skip("echter queued-Task vorhanden — Claim-Test übersprungen")`
aussteigen, wenn `n > 0`.

- [ ] **Schritt 2: Tests laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_queue.py::TestQuellenTrennung tests/test_forge_lebenszyklus.py -q`
Erwartet: FAIL — `TypeError: active() got an unexpected keyword argument 'quelle'`
und `AttributeError: module 'forge.queue' has no attribute 'TEST_QUELLE'`.

- [ ] **Schritt 3: Implementierung**

In `forge/queue.py` nach `AUTO_PARK_AFTER_ATTEMPTS`:

```python
# Quelle, deren Zeilen der Daemon nie anfasst. Zwei Testdateien
# (tests/test_forge_lebenszyklus.py, tests/test_forge_nachtlauf.py) legen echte
# Zeilen in forge_tasks an, und das Gate der Forge führt diese Tests im
# Worktree eines Tasks aus. Ohne diese Trennung könnte ein per SIGKILL
# beendeter Testlauf eine claimbare Zeile hinterlassen, die der nächste Tick
# mit echten LLM-Läufen bearbeitet — und umgekehrt könnte der Daemon einen
# Testtask claimen, während der Test noch läuft.
TEST_QUELLE = "test"


def _quellen_filter(quelle: str | None) -> tuple[str, tuple]:
    """SQL-Fragment und Parameter: Produktion (None) sieht alles ausser
    TEST_QUELLE, ein Test sieht ausschliesslich seine Quelle."""
    if quelle is None:
        return "AND source <> %s", (TEST_QUELLE,)
    return "AND source = %s", (quelle,)
```

`active` und `claim_next` ändern:

```python
def active(quelle: str | None = None) -> dict | None:
    """Der Task, der gerade in Arbeit ist — oder None.

    Pausierte Tasks (Rate-Limit, Not-Aus) gelten NICHT als aktiv, sonst würde
    eine Pause den Daemon für ihre gesamte Dauer blockieren. `quelle` siehe
    TEST_QUELLE.
    """
    filter_sql, filter_params = _quellen_filter(quelle)
    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = ANY(%s) AND (paused_until IS NULL OR paused_until <= NOW()) "
        f"{filter_sql} ORDER BY updated_at ASC LIMIT 1",
        (list(m.ACTIVE_STATES), *filter_params),
    )
    return rows[0] if rows else None


def claim_next(quelle: str | None = None) -> dict | None:
    # Docstring wie bisher, plus: `quelle` siehe TEST_QUELLE.
    laufend = active(quelle)
    if laufend is not None:
        return laufend

    filter_sql, filter_params = _quellen_filter(quelle)
    rows = db.query(
        "SELECT * FROM forge_tasks "
        "WHERE state = %s AND (paused_until IS NULL OR paused_until <= NOW()) "
        f"{filter_sql} ORDER BY priority DESC, id ASC LIMIT 1",
        (m.QUEUED, *filter_params),
    )
    # Rest unverändert
```

Der f-String enthält nur das feste Fragment aus `_quellen_filter`, nie einen
Wert — der Wert geht als Parameter. Das ist dieselbe Grenze wie bei
`_ARTEFAKT_FELDER` weiter unten in der Datei.

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_queue.py tests/test_forge_lebenszyklus.py -q`
Erwartet: PASS. Bestehende `TestActive`/`TestClaimNext`-Tests, die die
Parameterliste exakt vergleichen (`rec.queries[-1][1][0]`), bleiben gültig —
der Quellenparameter hängt hinten an.

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/queue.py tests/test_forge_queue.py tests/test_forge_lebenszyklus.py
git commit -m "feat(forge): Testzeilen (source='test') sind fuer den Daemon unsichtbar

active() und claim_next() filtern die Testquelle in der Produktion aus und
sehen im Test ausschliesslich sie. Ersetzt die zehnjaehrige Pause aus
Plan 2b durch eine Regel, die auch dann haelt, wenn das Gate der Forge die
DB-Tests im Worktree eines Tasks ausfuehrt — und die dem Simulator (Task 6)
erlaubt, seine Tasks ueber den echten claim_next zu ziehen."
```

---

### Task 2: Fix bleibt in REVIEWING, Runde zählt nach Erfolg

Heute geht ein erfolgreicher Fix nach `IMPLEMENTING`, die Implement-Stufe
läuft ein zweites Mal auf dem fertigen Baum, und erst danach reviewt jemand.
Die Runde wird vor dem Lauf gezählt — ein Rate-Limit im Fix verbraucht sie.
Der Nachtrag entscheidet beides anders.

**Files:**
- Modify: `forge/pipeline.py` (`_eine_stufe_intern`: Rundenprüfung vor dem
  Lauf, Stufenabschluss; beide `journal.log(... "stage_failed" ...)` im
  Kontingent-Zweig)
- Modify: `forge/queue.py` (`fixrunden`)
- Modify: `forge/models.py` (`REVIEWING`-Übergänge)
- Modify: `forge/journal.py` (`KINDS`)
- Modify: `tests/test_forge_pipeline.py`, `tests/test_forge_models.py`,
  `tests/test_forge_lebenszyklus.py`

**Interfaces:**
- Consumes: `queue.zaehle_fixrunde(task_id) -> int` (bestehend),
  `_verwirf_review_artefakte(worktree)` (bestehend)
- Produces: `queue.fixrunden(task_id) -> int` (nur lesen);
  Fix-Stufe liefert `"weiter"` **ohne** Zustandswechsel;
  `models._TRANSITIONS[REVIEWING] == {GATING} | _ESCAPES`;
  Journal-Kind `"kontingent"`

- [ ] **Schritt 1: Die fehlschlagenden Tests schreiben**

An `tests/test_forge_pipeline.py` anhängen (`_verdikt`, `stubs`, `RunResult`,
`json`, `m`, `pl` existieren dort):

```python
class TestFixBleibtInReviewing:
    """Nachtrag 2c: ein Fix wechselt keinen Zustand. Der nächste Tick trifft
    REVIEWING ohne Urteil an und lässt die echte Review-Stufe laufen."""

    def _fix_lauf(self, monkeypatch, stubs, tmp_path, ergebnis=None):
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda prompt, cwd, timeout, agent, model: ergebnis or RunResult(ok=True, text="egal")))
        monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
        monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
        return pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)

    def test_erfolgreicher_fix_wechselt_keinen_zustand(self, monkeypatch, stubs, tmp_path):
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert ergebnis == "weiter"
        assert stubs["states"] == [], f"Fix hat den Zustand gewechselt: {stubs['states']}"
        assert stubs["parks"] == []

    def test_nach_dem_fix_ist_das_urteil_weg_und_review_folgt(self, monkeypatch, stubs, tmp_path):
        self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert not (tmp_path / pl.stages.VERDIKT_DATEI).is_file()
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is False and stufe.name == "review"

    def test_erfolgreicher_fix_zaehlt_genau_eine_runde(self, monkeypatch, stubs, tmp_path):
        self._fix_lauf(monkeypatch, stubs, tmp_path)
        assert stubs["fixrunden"] == 1

    def test_rate_limit_im_fix_verbraucht_keine_runde(self, monkeypatch, stubs, tmp_path):
        """Fund I4 aus dem 2b-Review: zwei Rate-Limits im Fix parkten den Task
        mit 'Fix-Runden-Grenze erreicht', ohne dass je ein Fix lief."""
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: False)
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path,
                                  RunResult(ok=False, rate_limited=True, error="429"))
        assert ergebnis == "fehler"
        assert stubs["fixrunden"] == 0
        assert (tmp_path / pl.stages.VERDIKT_DATEI).is_file(), "Urteil weg, obwohl kein Fix lief"

    def test_fehlgeschlagener_fix_verbraucht_keine_runde(self, monkeypatch, stubs, tmp_path):
        ergebnis = self._fix_lauf(monkeypatch, stubs, tmp_path,
                                  RunResult(ok=False, error="kaputt"))
        assert ergebnis == "geparkt"
        assert stubs["fixrunden"] == 0

    def test_grenze_wird_vor_dem_lauf_gelesen_nicht_gezaehlt(self, monkeypatch, stubs, tmp_path):
        stubs["fixrunden"] = pl.MAX_FIXRUNDEN
        gelaufen = []
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda *a, **k: gelaufen.append(1) or RunResult(ok=True, text="egal")))
        _verdikt(tmp_path, "fail", [{"severity": "critical", "what": "x"}])
        ergebnis = pl._eine_stufe_intern({"id": 1}, 1, m.REVIEWING, tmp_path)
        assert ergebnis == "geparkt"
        assert gelaufen == [], "Fix lief trotz erreichter Grenze"
        assert stubs["fixrunden"] == pl.MAX_FIXRUNDEN, "Grenzprüfung hat gezählt"
        assert "Fix-Runden-Grenze" in stubs["parks"][-1][1]


class TestKontingentJournalKind:
    def test_erschoepfte_kette_journalt_als_kontingent(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.ketten, "waehle", lambda name, verboten=frozenset(): None)
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        kinds = [a[1] for a, kw in stubs["journal"]]
        assert "kontingent" in kinds
        assert "stage_failed" not in kinds, "Kontingent zählt im Journal als Fehler"

    def test_rate_limit_das_die_kette_leert_journalt_als_kontingent(self, monkeypatch, stubs, tmp_path):
        monkeypatch.setattr(pl.ketten, "kette_erschoepft", lambda name: True)
        monkeypatch.setattr(pl.backends, "hole", lambda name: (
            lambda *a, **k: RunResult(ok=False, rate_limited=True, error="429")))
        pl._eine_stufe_intern({"id": 1}, 1, m.SPECCING, tmp_path)
        kinds = [a[1] for a, kw in stubs["journal"]]
        assert "kontingent" in kinds
```

Die `stubs`-Fixture braucht dafür einen Stub für die neue Lesefunktion. In
der Fixture direkt nach `monkeypatch.setattr(pl.queue, "zaehle_fixrunde", _fixrunde)`:

```python
    # Nachtrag 2c: die Grenzprüfung liest nur, gezählt wird nach dem Lauf.
    monkeypatch.setattr(pl.queue, "fixrunden", lambda tid: aufz["fixrunden"])
```

An `tests/test_forge_models.py` anhängen:

```python
class TestFixSchleifeOhneImplementReRun:
    def test_reviewing_geht_nicht_mehr_nach_implementing(self):
        """Nachtrag 2c: der Fix bleibt in REVIEWING; der Übergang wäre ungenutzt,
        und ungenutzte Übergänge sind das, was die Fix-Schleife bis 2b
        unerreichbar gemacht hat."""
        assert not m.can_transition(m.REVIEWING, m.IMPLEMENTING)

    def test_reviewing_geht_weiterhin_nach_gating(self):
        assert m.can_transition(m.REVIEWING, m.GATING)
```

In `tests/test_forge_lebenszyklus.py`, Test `test_negatives_review_landet_in_der_fix_runde`:
die letzten drei Zeilen (nach dem zweiten `pl._eine_stufe_intern`-Aufruf)
ersetzen durch:

```python
        assert _zustand(task_id) == m.REVIEWING, "Fix hat den Zustand gewechselt"
        assert not (tmp_path / stages.VERDIKT_DATEI).is_file(), \
            "altes Urteil liegt nach dem Fix noch im Worktree"
        stufe, ist_fix = pl._waehle_stufe(m.REVIEWING, tmp_path)
        assert ist_fix is False and stufe.name == "review", "nach dem Fix folgt kein Review"
        zeile = db.query_one("SELECT refusals FROM forge_tasks WHERE id=%s", (task_id,))
        assert zeile["refusals"] == 1
```

- [ ] **Schritt 2: Tests laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py::TestFixBleibtInReviewing tests/test_forge_pipeline.py::TestKontingentJournalKind tests/test_forge_models.py::TestFixSchleifeOhneImplementReRun tests/test_forge_lebenszyklus.py -q`
Erwartet: FAIL — `states == [(1, 'implementing')]`, `fixrunden == 1` bei
Rate-Limit, `can_transition(REVIEWING, IMPLEMENTING)` ist `True`,
`"stage_failed"` im Journal.

- [ ] **Schritt 3: Implementierung**

`forge/queue.py`, vor `zaehle_fixrunde`:

```python
def fixrunden(task_id: int) -> int:
    """Wie viele Fix-Runden dieser Task verbraucht hat — nur lesen.

    Nachtrag 2c: die Grenzprüfung VOR dem Fix-Lauf liest, gezählt wird erst
    NACH einem erfolgreichen Lauf (zaehle_fixrunde). Sonst verbraucht ein
    Rate-Limit oder Absturz im Fix eine Runde, ohne dass je ein Fix lief.
    """
    zeile = db.query_one("SELECT refusals FROM forge_tasks WHERE id=%s", (task_id,))
    return int(zeile["refusals"]) if zeile else 0
```

`forge/models.py`:

```python
    # Nachtrag 2c: ein Fix bleibt in REVIEWING (kein Zustandswechsel, altes
    # Urteil wird verworfen, nächster Tick reviewt). Der frühere Übergang
    # REVIEWING -> IMPLEMENTING liess die Implement-Stufe nach jedem Fix ein
    # zweites Mal laufen und ist ersatzlos entfernt.
    REVIEWING: {GATING} | _ESCAPES,
```

`forge/journal.py`, in `KINDS` ergänzen: `"kontingent"`.

`forge/pipeline.py`, die Rundenprüfung vor dem Lauf:

```python
    if ist_fix:
        # Nachtrag 2c: nur lesen. Gezählt wird nach einem erfolgreichen Lauf —
        # ein Rate-Limit, ein Timeout ohne Produkt oder ein Absturz im Fix
        # verbraucht keine Runde (Fund I4, Abschluss-Review Plan 2b).
        runden = queue.fixrunden(task_id)
        if runden >= MAX_FIXRUNDEN:
            _park(task_id, state,
                  f"Fix-Runden-Grenze erreicht ({runden} von {MAX_FIXRUNDEN} verbraucht), Task {task_id}")
            return "geparkt"
```

Beide `journal.log(task_id, "stage_failed", grund)` in den Kontingent-Zweigen
(Kettenwahl ohne Glied; Rate-Limit, das die Kette leert) auf
`journal.log(task_id, "kontingent", grund)` ändern. Im Rate-Limit-Zweig steht
heute nur ein `return`; dort vor dem `return` ergänzen:

```python
    if ergebnis.rate_limited:
        if ketten.kette_erschoepft(stufe.name):
            journal.log(task_id, "kontingent",
                        f"Rate-Limit hat die Kette für Stufe '{stufe.name}' geleert — "
                        f"Task {task_id} bleibt liegen")
            return "kontingent"
        return "fehler"
```

Den Stufenabschluss (ab dem Kommentarblock „Die Fix-Stufe teilt sich REVIEWING
…" bis zum letzten `return`) ersetzen durch:

```python
    # Ein negatives Review rückt NICHT vor (Plan 2b): der Task bleibt in
    # REVIEWING, und der nächste Durchlauf trifft ihn mit vorliegendem
    # negativem Urteil an — die Bedingung, auf die _waehle_stufe wartet.
    if stufe.name == "review" and _hat_negatives_verdikt(worktree):
        journal.log(task_id, "stage_done",
                    f"Review negativ für Task {task_id} — Fix-Runde folgt")
        return "weiter"

    if ist_fix:
        # Nachtrag 2c: ein Fix wechselt keinen Zustand. Das alte Urteil (und
        # der Diff, den es beurteilt hat) werden verworfen; der nächste Tick
        # trifft REVIEWING ohne Urteil an und lässt die echte Review-Stufe
        # laufen. Vorher ging der Fix nach IMPLEMENTING und die
        # Implement-Stufe lief ein zweites Mal auf dem fertigen Baum — jede
        # Runde kostete fix + implement + review.
        #
        # Reihenfolge: erst verwerfen, dann zählen. Stirbt der Prozess
        # dazwischen, fehlt eine Zählung (harmlos); umgekehrt läge ein
        # veraltetes Urteil neben einem gezählten Fix, und der nächste Tick
        # würde erneut fixen statt reviewen.
        _verwirf_review_artefakte(worktree)
        runden = queue.zaehle_fixrunde(task_id)
        queue.versuche_zuruecksetzen(task_id)
        journal.log(task_id, "stage_done",
                    f"Fix-Runde {runden} von {MAX_FIXRUNDEN} abgeschlossen für Task {task_id} — "
                    f"erneutes Review folgt")
        return "weiter"

    ziel = stufe.next_state
    if not queue.set_state(task_id, ziel, current=state):
        # Compare-and-Swap ist fehlgeschlagen (verbotener Übergang oder ein
        # anderer Schreiber war schneller) — der Task steckt tatsächlich noch
        # in `state`, und "weiter"/"fertig" zurückzugeben würde dem Aufrufer
        # einen Fortschritt vorgaukeln, der nie stattfand.
        _park(task_id, state,
              f"Zustandswechsel {state} -> {ziel} schlug fehl (Task {task_id})")
        return "geparkt"
    # Die Stufe hat sauber abgeschlossen — der Fehlschlag-Zähler beginnt neu
    # (forge/queue.py: zaehle_fehlschlag/versuche_zuruecksetzen).
    queue.versuche_zuruecksetzen(task_id)
    if stufe.name == "implement":
        # Fund F3 (Abschluss-Review Plan 2b): ein geparkter Task, den jemand
        # mit demselben Worktree neu einreiht, trägt dort noch das alte
        # Urteil, während der Code sich seither geändert hat. Beim Abschluss
        # der Implement-Stufe ist jedes vorliegende Urteil per Definition
        # älter als der Code, den es beurteilen müsste. `missing_ok=True`
        # macht den normalen ersten Durchlauf zum No-op.
        _verwirf_review_artefakte(worktree)
    return "fertig" if ziel == m.GATING else "weiter"
```

Im Modul-Docstring von `forge/pipeline.py` die Zeile zu `"weiter"` ergänzen:
„… oder ein Fix ist abgeschlossen (kein Übergang, Review folgt)". Die Zeile
zu `"fertig"` von „Plan 3 übernimmt (Merge/Restart)" auf „der Daemon lässt
das Gate laufen" ändern.

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_pipeline.py tests/test_forge_models.py tests/test_forge_lebenszyklus.py -q`
Erwartet: die neuen Tests PASS. **Bestehende Tests, die vom alten Verhalten
ausgingen, schlagen jetzt fehl — das ist der Befund.** Bekannt:
`TestKritisch1FixRundeErzwingtErneutesReview` (drei Ticks fix → implement →
review; jetzt sind es zwei: fix → review), Tests in `TestFixrunden`/ähnlich,
die `zaehle_fixrunde` vor dem Lauf erwarten oder auf `(1, m.IMPLEMENTING)` in
`stubs["states"]` prüfen, `test_forge_models`-Tests, die
`REVIEWING → IMPLEMENTING` als erlaubt listen. Jeden so anpassen, dass er die
**neue** Eigenschaft prüft (Fix → kein Zustandswechsel, Urteil weg, Review
folgt; Zählung nach Erfolg), und jede Anpassung im Report begründen. Kein
Test wird gelöscht, ohne dass ein neuer dieselbe Eigenschaft abdeckt.

- [ ] **Schritt 5: Mutationsnachweis**

Drei Mutationen, je einzeln, danach `git checkout -- forge/pipeline.py forge/models.py`
und `git status --short` leer (bis auf die eigenen Testdateien):

1. `_verwirf_review_artefakte(worktree)` im `ist_fix`-Zweig entfernen →
   `test_nach_dem_fix_ist_das_urteil_weg_und_review_folgt` rot.
2. `queue.zaehle_fixrunde(task_id)` wieder **vor** den Lauf ziehen (statt
   `fixrunden`) → `test_rate_limit_im_fix_verbraucht_keine_runde` rot.
3. `REVIEWING: {GATING, IMPLEMENTING} | _ESCAPES` in models.py →
   `test_reviewing_geht_nicht_mehr_nach_implementing` rot.

Ausgaben in den Report.

- [ ] **Schritt 6: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 7: Committen**

```bash
git add forge/pipeline.py forge/queue.py forge/models.py forge/journal.py tests/test_forge_pipeline.py tests/test_forge_models.py tests/test_forge_lebenszyklus.py
git commit -m "fix(forge): Fix bleibt in REVIEWING, Runde zaehlt erst nach Erfolg

Ein erfolgreicher Fix wechselt keinen Zustand mehr: altes Urteil weg,
naechster Tick reviewt. Vorher ging er nach IMPLEMENTING und die
Implement-Stufe lief ein zweites Mal auf dem fertigen Baum. Der Uebergang
REVIEWING -> IMPLEMENTING ist ersatzlos entfernt.

Die Fix-Runden-Grenze liest vor dem Lauf und zaehlt nach ok=True —
Rate-Limit, Timeout ohne Produkt und Absturz verbrauchen keine Runde.
'kontingent' hat einen eigenen Journal-Kind statt stage_failed."
```

---

### Task 3: `AWAITING_APPROVAL` statt `AWAITING_RESTART`

Ein grünes Gate legt den Task heute nach `awaiting_restart_window`, einen
Zustand, der für einen Daemon-Neustart nach Merges in `forge/` gedacht war.
Die Agenten dürfen `forge/` nicht anfassen; der Zustand ist tot. Der Nachtrag
ersetzt ihn durch den Wartezustand für Timos Freigabe.

**Files:**
- Modify: `forge/models.py`
- Modify: `forge/daemon.py` (`_gate_und_abschliessen`, Modul-Docstring)
- Modify: `tests/test_forge_models.py`, `tests/test_forge_daemon.py`

**Interfaces:**
- Produces: `models.AWAITING_APPROVAL = "awaiting_approval"`;
  `models.AWAITING_RESTART` **entfernt**;
  `ACTIVE_STATES = {SPECCING, PLANNING, IMPLEMENTING, REVIEWING, GATING}`;
  Übergänge `GATING → {AWAITING_APPROVAL} | _ESCAPES`,
  `AWAITING_APPROVAL → {MERGED, PARKED}`; `GATING → MERGED` **entfernt**
  (kein automatischer Merge, auch nicht für Docs — Spec „Bewusst nicht enthalten")

- [ ] **Schritt 1: Die fehlschlagenden Tests schreiben**

An `tests/test_forge_models.py` anhängen:

```python
class TestZustandNachDemGate:
    """Nachtrag 2c: GATING -> AWAITING_APPROVAL -> MERGED | PARKED."""

    def test_gruenes_gate_wartet_auf_freigabe(self):
        assert m.can_transition(m.GATING, m.AWAITING_APPROVAL)

    def test_kein_automatischer_merge_aus_gating(self):
        """Spec, 'Bewusst nicht enthalten': kein automatischer Merge, auch
        nicht für Docs."""
        assert not m.can_transition(m.GATING, m.MERGED)

    def test_freigabe_fuehrt_zu_merged_oder_parked(self):
        assert m.can_transition(m.AWAITING_APPROVAL, m.MERGED)
        assert m.can_transition(m.AWAITING_APPROVAL, m.PARKED)
        assert not m.can_transition(m.AWAITING_APPROVAL, m.QUEUED)

    def test_wartender_task_blockiert_keine_bahn(self):
        assert m.AWAITING_APPROVAL not in m.ACTIVE_STATES

    def test_awaiting_restart_gibt_es_nicht_mehr(self):
        assert not hasattr(m, "AWAITING_RESTART")
        assert "awaiting_restart_window" not in m._TRANSITIONS
```

In `tests/test_forge_daemon.py` den Test
`test_gruenes_gate_geht_in_awaiting_restart` umbenennen in
`test_gruenes_gate_geht_in_awaiting_approval` und die Assertion auf
`[(10, m.AWAITING_APPROVAL, m.GATING)]` ändern; den Klassen-Docstring
(Zeile ~113) entsprechend.

- [ ] **Schritt 2: Tests laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_models.py::TestZustandNachDemGate tests/test_forge_daemon.py -q`
Erwartet: FAIL — `AttributeError: module 'forge.models' has no attribute 'AWAITING_APPROVAL'`.

- [ ] **Schritt 3: Implementierung**

`forge/models.py`: `AWAITING_RESTART = "awaiting_restart_window"` ersetzen durch

```python
# Nachtrag 2c: das grüne Gate wartet hier auf Timos Freigabe (forge/cli.py,
# ab Plan 3 der Telegram-Bot). Gehört bewusst NICHT zu ACTIVE_STATES — ein
# Task, der auf Timo wartet, blockiert keine Bahn. Der frühere Zustand
# awaiting_restart_window (Daemon-Neustart nach Merges in forge/) ist
# entfernt: die Agenten dürfen forge/ nicht anfassen, kein Merge braucht
# einen Neustart, und am 2026-09-14 stand keine Zeile in dem Zustand.
AWAITING_APPROVAL = "awaiting_approval"
```

`ACTIVE_STATES` ohne `AWAITING_RESTART`. Übergänge:

```python
    # Kein automatischer Merge aus GATING, auch nicht für Docs (Spec,
    # "Bewusst nicht enthalten"). Grün heisst: warten auf Timo.
    GATING: {AWAITING_APPROVAL} | _ESCAPES,
    AWAITING_APPROVAL: {MERGED, PARKED},
```

`forge/daemon.py`, `_gate_und_abschliessen`: `m.AWAITING_RESTART` durch
`m.AWAITING_APPROVAL` ersetzen (zwei Stellen), die Journal-Nachricht auf
`"Gate bestanden — wartet auf Freigabe (forge.cli approve)"`, Docstring der
Funktion und des Moduls entsprechend („grün bringt ihn nach
`awaiting_approval`, wo er auf Timos Freigabe wartet").

Nach der Änderung `grep -rn "AWAITING_RESTART\|awaiting_restart" forge/ tests/`
— muss leer sein.

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_models.py tests/test_forge_daemon.py -q`
Erwartet: PASS. Bestehende Modell-Tests, die `AWAITING_RESTART` in
Übergangslisten führen (z.B. `tests/test_forge_models.py:45`), anpassen und im
Report nennen.

- [ ] **Schritt 5: Mutationsnachweis**

`GATING: {AWAITING_APPROVAL, MERGED} | _ESCAPES` →
`test_kein_automatischer_merge_aus_gating` rot. Zurückdrehen, Ausgabe in den
Report.

- [ ] **Schritt 6: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 7: Committen**

```bash
git add forge/models.py forge/daemon.py tests/test_forge_models.py tests/test_forge_daemon.py
git commit -m "feat(forge): gruenes Gate wartet in AWAITING_APPROVAL auf Timos Freigabe

Ersetzt awaiting_restart_window, das fuer Daemon-Neustarts nach Merges in
forge/ gedacht war — die Agenten duerfen forge/ nicht anfassen, der
Zustand war tot, und keine Zeile stand darin. Der direkte Uebergang
GATING -> MERGED faellt weg: kein automatischer Merge, auch nicht fuer
Docs."
```

---

### Task 4: Nachtfenster und weicher Stop

Heute läuft der Daemon mit `RunAtLoad` + `KeepAlive` rund um die Uhr, sobald
die plist geladen ist. Der Nachtrag will: Start 23:00 per Kalender, Ende
07:00 durch den Daemon selbst, weicher Stop per Datei.

**Files:**
- Modify: `forge/daemon.py` (Konstanten, `im_nachtfenster`, `halt_angefordert`,
  `main`)
- Create: `forge/launchd/com.mantis.forge.plist`
- Modify: `tests/test_forge_daemon.py`

**Interfaces:**
- Produces: `daemon.NACHT_BEGINN_STUNDE = 23`, `daemon.NACHT_ENDE_STUNDE = 7`,
  `daemon.HALT_FILE = Path.home() / ".mantis-forge-halt"`,
  `daemon.im_nachtfenster(jetzt: datetime | None = None) -> bool`,
  `daemon.halt_angefordert() -> bool`. `main()` kehrt ausserhalb des Fensters
  und bei Halt mit `return` zurück (Exit-Code 0).

- [ ] **Schritt 1: Die fehlschlagenden Tests schreiben**

An `tests/test_forge_daemon.py` anhängen (`d`, `m`, `pytest` existieren dort;
das `_Halt`-Muster stammt aus `TestKontingentImDaemon`):

```python
from datetime import datetime


class TestNachtfenster:
    def test_23_uhr_ist_drin(self):
        assert d.im_nachtfenster(datetime(2026, 9, 14, 23, 0)) is True

    def test_3_uhr_ist_drin(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 3, 30)) is True

    def test_7_uhr_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 7, 0)) is False

    def test_mittag_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 15, 12, 0)) is False

    def test_22_59_ist_draussen(self):
        assert d.im_nachtfenster(datetime(2026, 9, 14, 22, 59)) is False


class TestFensterUndHaltImMain:
    def _main_mit(self, monkeypatch, tmp_path, fenster, halt_datei_da, ticks):
        """Lässt main() laufen; `fenster` ist die Folge der Antworten von
        im_nachtfenster(), `ticks` zählt die tick()-Aufrufe."""
        antworten = iter(fenster)

        class _Halt(BaseException):
            pass

        def _tick():
            ticks.append(1)
            if len(ticks) > 10:
                raise _Halt
            return "leerlauf"

        halt = tmp_path / "halt"
        if halt_datei_da:
            halt.write_text("stop")
        monkeypatch.setattr(d, "HALT_FILE", halt)
        monkeypatch.setattr(d, "STOP_FILE", tmp_path / "stop")
        monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: next(antworten))
        monkeypatch.setattr(d, "tick", _tick)
        monkeypatch.setattr(d.time, "sleep", lambda s: None)
        monkeypatch.setattr(d.journal, "log", lambda *a, **k: None)
        monkeypatch.setattr(d.db, "init_pool", lambda *a, **kw: None)
        monkeypatch.setattr(d.db, "run_migrations", lambda *a, **kw: None)
        try:
            d.main()
            return "beendet", halt
        except _Halt:
            return "laeuft_noch", halt

    def test_ausserhalb_des_fensters_kein_tick(self, monkeypatch, tmp_path):
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [False], False, ticks)
        assert ergebnis == "beendet"
        assert ticks == []

    def test_fensterende_beendet_nach_dem_laufenden_tick(self, monkeypatch, tmp_path):
        """Die laufende Stufe läuft zu Ende; erst der nächste Durchlauf prüft
        das Fenster."""
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [True, True, False], False, ticks)
        assert ergebnis == "beendet"
        assert len(ticks) == 2

    def test_halt_datei_beendet_und_wird_geloescht(self, monkeypatch, tmp_path):
        ticks = []
        ergebnis, halt = self._main_mit(monkeypatch, tmp_path, [True] * 5, True, ticks)
        assert ergebnis == "beendet"
        assert ticks == [], "Halt lag vor dem ersten Tick vor — kein Tick erlaubt"
        assert not halt.exists(), "Halt-Datei überlebt das Beenden"

    def test_ohne_halt_und_im_fenster_laeuft_es(self, monkeypatch, tmp_path):
        ticks = []
        ergebnis, _ = self._main_mit(monkeypatch, tmp_path, [True] * 20, False, ticks)
        assert ergebnis == "laeuft_noch"
        assert len(ticks) == 11
```

- [ ] **Schritt 2: Tests laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_daemon.py::TestNachtfenster tests/test_forge_daemon.py::TestFensterUndHaltImMain -q`
Erwartet: FAIL — `AttributeError: module 'forge.daemon' has no attribute 'im_nachtfenster'`.

- [ ] **Schritt 3: Implementierung**

`forge/daemon.py`, bei den Konstanten (Import `from datetime import datetime` ergänzen):

```python
# Nachtrag 2c: launchd startet um NACHT_BEGINN_STUNDE (forge/launchd/
# com.mantis.forge.plist, StartCalendarInterval), der Daemon beendet sich
# selbst ab NACHT_ENDE_STUNDE. Ein Task, der beim Fensterende aktiv ist,
# bleibt aktiv — die nächste Nacht setzt auf derselben Stufe auf, genau wie
# nach einem Absturz.
NACHT_BEGINN_STUNDE = 23
NACHT_ENDE_STUNDE = 7
# Weicher Stop (forge.cli stop): der laufende Tick endet, dann der Daemon.
# Anders als STOP_FILE wird die Datei beim Beenden gelöscht — sie ist eine
# Bitte, kein Not-Aus.
HALT_FILE = Path.home() / ".mantis-forge-halt"


def im_nachtfenster(jetzt: datetime | None = None) -> bool:
    """23:00 bis 06:59 — die Stunden, in denen die Forge arbeitet."""
    jetzt = jetzt or datetime.now()
    return jetzt.hour >= NACHT_BEGINN_STUNDE or jetzt.hour < NACHT_ENDE_STUNDE


def halt_angefordert() -> bool:
    return HALT_FILE.exists()
```

In `main()`, am Anfang des `while True:`-Körpers, **vor** `should_run`:

```python
        if not im_nachtfenster():
            journal.log(None, "daemon_stop",
                        f"Nachtfenster zu Ende ({NACHT_ENDE_STUNDE}:00) — Daemon beendet sich, "
                        f"launchd startet um {NACHT_BEGINN_STUNDE}:00 neu")
            log.info("Forge: Nachtfenster zu Ende")
            return
        if halt_angefordert():
            journal.log(None, "daemon_stop", "Weicher Stop angefordert (forge.cli stop) — Daemon beendet sich")
            log.info("Forge: weicher Stop")
            HALT_FILE.unlink(missing_ok=True)
            return
```

Den Kommentar bei `STOP_FILE.write_text(...)` („Der Job läuft mit
KeepAlive=true …") anpassen: launchd startet nicht mehr per KeepAlive neu,
sondern um 23:00 — die Not-Aus-Datei muss **diesen** Neustart überleben, der
Satz bleibt sinngemäss wahr. Modul-Docstring: den Absatz „Der Daemon hält sich
an zwei Bremsen" um Nachtfenster und weichen Stop ergänzen.

`forge/launchd/com.mantis.forge.plist` (neu — die Version unter
`~/Library/LaunchAgents/` ist der alte Dauerlauf mit `RunAtLoad`+`KeepAlive`
und wird durch diese ersetzt):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mantis.forge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14</string>
        <string>-u</string>
        <string>-m</string>
        <string>forge.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/timoegersdorfer/Mantis</string>
    <!-- Ein launchd-User-Agent bekommt nur PATH=/usr/bin:/bin:/usr/sbin:/sbin.
         opencode und agy liegen unter ~/.local/bin bzw. /opt/homebrew/bin —
         ohne diesen Eintrag scheitert jeder Backend-Aufruf mit
         FileNotFoundError, und jeder Task wird mit null Tokens geparkt. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/timoegersdorfer/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <!-- Nachtrag 2c: Kalenderstart statt Dauerlauf. Der Daemon beendet sich
         selbst ab 07:00 (forge/daemon.py, im_nachtfenster) und wird nicht
         neu gestartet — KeepAlive würde ihn 30 s später wieder hochziehen. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>23</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/mantis_forge_out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mantis_forge_err.log</string>
</dict>
</plist>
```

Dazu eine Datei `forge/launchd/README.md` mit den drei Installationszeilen:

```
launchctl bootout gui/$(id -u)/com.mantis.forge 2>/dev/null
cp forge/launchd/com.mantis.forge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mantis.forge.plist
```

Die Installation selbst ist **nicht** Teil dieses Tasks — sie wirkt ausserhalb
des Worktrees und ist Timos Schritt (siehe „Danach").

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_daemon.py -q`
Erwartet: PASS. Bestehende `main()`-Tests laufen ohne `im_nachtfenster`-Stub
gegen die echte Uhr — tagsüber würden sie sofort beenden. Deshalb in **jedem**
bestehenden `main()`-Test `monkeypatch.setattr(d, "im_nachtfenster", lambda jetzt=None: True)`
und `monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")` ergänzen (bzw.
`tmp_path` als Fixture aufnehmen). Betroffene Tests im Report auflisten.

- [ ] **Schritt 5: Mutationsnachweis**

Den `if not im_nachtfenster():`-Block entfernen →
`test_ausserhalb_des_fensters_kein_tick` rot. Den `HALT_FILE.unlink` entfernen
→ `test_halt_datei_beendet_und_wird_geloescht` rot. Zurückdrehen, Ausgaben in
den Report.

- [ ] **Schritt 6: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 7: Committen**

```bash
git add forge/daemon.py forge/launchd/com.mantis.forge.plist forge/launchd/README.md tests/test_forge_daemon.py
git commit -m "feat(forge): Nachtfenster 23-07 und weicher Stop per ~/.mantis-forge-halt

launchd startet per Kalender um 23:00 statt RunAtLoad+KeepAlive; der
Daemon beendet sich ab 07:00 selbst, ein aktiver Task bleibt aktiv und
die naechste Nacht setzt auf. forge.cli stop schreibt die Halt-Datei,
der laufende Tick endet, dann der Daemon; die Datei wird dabei geloescht."
```

---

### Task 5: Freigabe, Morgenbericht, CLI

Ohne Telegram-Bot (Plan 3) braucht die Nacht einen Weg, morgens
freizugeben. `forge/freigabe.py` hält die Logik, `forge/bericht.py` den Text,
`forge/cli.py` ist die dünne Hülle — Plan 3 ruft dieselben Funktionen aus dem
Bot.

**Files:**
- Modify: `forge/queue.py` (`hole`, `nach_zustand`, `requeue`)
- Create: `forge/freigabe.py`, `forge/bericht.py`, `forge/cli.py`
- Test: `tests/test_forge_queue.py`, `tests/test_forge_freigabe.py`,
  `tests/test_forge_bericht.py`, `tests/test_forge_cli.py`

**Interfaces:**
- Consumes: `gitctl.run(*args, cwd=Path, timeout=300) -> CompletedProcess`;
  `worktree.branch_for(task_id) -> str`, `worktree.path_for(task_id) -> Path`,
  `worktree.remove(task_id, repo=None) -> bool`; `budget.stand() -> list[dict]`;
  `journal.recent(limit) -> list[dict]`; `daemon.HALT_FILE`; `pipeline.BASIS_BRANCH`
- Produces: `queue.hole(task_id) -> dict | None`;
  `queue.nach_zustand(state: str, quelle=None) -> list[dict]` (Produktionsfilter
  wie `active`);
  `queue.requeue(task_id) -> bool` (aus PARKED/FAILED nach QUEUED, setzt
  `attempts`, `refusals`, `parked_reason` zurück);
  `freigabe.freigeben(task_id, repo=None) -> str` mit Rückgabe
  `"gemerged" | "konflikt" | "abgelehnt"`;
  `freigabe.ablehnen(task_id, grund) -> bool`;
  `freigabe.neu_einreihen(task_id) -> bool`;
  `bericht.morgenbericht() -> str`;
  `cli.main(argv: list[str] | None = None) -> int`

- [ ] **Schritt 1: Die fehlschlagenden Tests schreiben**

An `tests/test_forge_queue.py` anhängen:

```python
class TestHoleUndNachZustand:
    def test_hole_liefert_die_zeile(self, monkeypatch):
        _patch(monkeypatch, rows=[{"id": 7, "state": m.PARKED}])
        assert q.hole(7)["id"] == 7

    def test_hole_ohne_treffer_none(self, monkeypatch):
        _patch(monkeypatch, rows=[])
        assert q.hole(7) is None

    def test_nach_zustand_filtert_die_testquelle_aus(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[])
        q.nach_zustand(m.AWAITING_APPROVAL)
        sql, params = rec.queries[-1]
        assert "source <> %s" in sql and params == (m.AWAITING_APPROVAL, q.TEST_QUELLE)


class TestRequeue:
    def test_setzt_zaehler_und_grund_zurueck(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"id": 7, "state": m.PARKED}])
        assert q.requeue(7) is True
        sql, params = rec.executes[-1]
        assert "attempts=0" in sql and "refusals=0" in sql and "parked_reason=NULL" in sql
        assert "state=%s" in sql and params[0] == m.QUEUED
        assert "AND state=%s" in sql, "kein Compare-and-Swap"

    def test_nur_aus_parked_oder_failed(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"id": 7, "state": m.REVIEWING}])
        assert q.requeue(7) is False
        assert rec.executes == []
```

`tests/test_forge_freigabe.py` (neu):

```python
"""Freigabe: der Weg von AWAITING_APPROVAL nach MERGED — oder zurück zu Timo.

Gegen gemocktes git (Vertrag) und einmal gegen ein echtes tmp-Repo
(Integration), wie TestC3GegenEchtesGit in test_forge_pipeline.py.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import freigabe as f
from forge import models as m


class _Git:
    """Antwortet auf gitctl.run nach einem Drehbuch. Schlüssel ist das erste
    Argument (git-Unterkommando); der Wert ist (returncode, stdout), eine
    Liste davon (wird der Reihe nach verbraucht, der letzte Eintrag gilt
    weiter) oder eine Funktion args -> (returncode, stdout)."""

    def __init__(self, antworten: dict):
        self.antworten = antworten
        self.aufrufe: list[tuple] = []

    def __call__(self, *args, cwd=None, timeout=300):
        self.aufrufe.append((args, cwd))
        wert = self.antworten.get(args[0], (0, ""))
        if isinstance(wert, list):
            wert = wert.pop(0) if len(wert) > 1 else wert[0]
        if callable(wert):
            wert = wert(args)
        rc, out = wert
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="CONFLICT" if rc else "")


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """Task 7 in AWAITING_APPROVAL, alle Seiteneffekte aufgezeichnet."""
    aufz = {"states": [], "parks": [], "journal": [], "worktree_removed": []}
    monkeypatch.setattr(f.queue, "hole", lambda tid: {"id": tid, "state": m.AWAITING_APPROVAL, "title": "T"})
    monkeypatch.setattr(f.queue, "set_state",
                        lambda tid, target, current: aufz["states"].append((tid, target, current)) or True)
    monkeypatch.setattr(f.queue, "park",
                        lambda tid, current, reason: aufz["parks"].append((tid, reason)) or True)
    monkeypatch.setattr(f.journal, "log", lambda *a, **k: aufz["journal"].append(a))
    monkeypatch.setattr(f.worktree, "remove", lambda tid, repo=None: aufz["worktree_removed"].append(tid) or True)
    monkeypatch.setattr(f.worktree, "path_for", lambda tid: tmp_path / f"task-{tid}")
    return aufz


def _git(monkeypatch, antworten):
    git = _Git(antworten)
    monkeypatch.setattr(f.gitctl, "run", git)
    return git


class TestFreigebenVertrag:
    def test_sauberer_fall_merged_und_raeumt_auf(self, monkeypatch, welt, tmp_path):
        """merge-base == main-Spitze: kein Nachziehen nötig."""
        # rev-parse wird zweimal gefragt: Branchname von HEAD, dann die Spitze von main.
        git = _git(monkeypatch, {"status": (0, ""),
                                 "rev-parse": [(0, "main\n"), (0, "abc\n")],
                                 "merge-base": (0, "abc\n")})

        assert f.freigeben(7, repo=tmp_path) == "gemerged"
        assert welt["states"] == [(7, m.MERGED, m.AWAITING_APPROVAL)]
        assert welt["worktree_removed"] == [7]
        merges = [a for a, _ in git.aufrufe if a[0] == "merge"]
        assert merges == [("merge", "--no-ff", "--no-edit", "forge/task-7")]
        assert ("branch", "-d", "forge/task-7") in [a for a, _ in git.aufrufe]

    def test_schmutziges_main_lehnt_ab_ohne_zu_parken(self, monkeypatch, welt, tmp_path):
        _git(monkeypatch, {"status": (0, " M forge/x.py\n"), "rev-parse": (0, "main\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == [] and welt["parks"] == []

    def test_hauptrepo_nicht_auf_main_lehnt_ab(self, monkeypatch, welt, tmp_path):
        _git(monkeypatch, {"status": (0, ""), "rev-parse": (0, "forge/pipeline\n")})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"
        assert welt["states"] == []

    def test_main_bewegt_und_nachziehen_konfliktiert_parkt(self, monkeypatch, welt, tmp_path):
        git = _git(monkeypatch, {"status": (0, ""),
                                 "rev-parse": [(0, "main\n"), (0, "neu\n")],
                                 "merge-base": (0, "alt\n"),
                                 "merge": lambda args: (0, "") if "--abort" in args else (1, "")})

        assert f.freigeben(7, repo=tmp_path) == "konflikt"
        assert welt["parks"] and "main hat sich bewegt" in welt["parks"][0][1]
        assert welt["states"] == []
        merges = [(a, cwd) for a, cwd in git.aufrufe if a[0] == "merge"]
        # Nachziehen im Worktree, dann abgebrochen — nie ein Merge in main.
        assert merges[0] == (("merge", "--no-edit", "main"), tmp_path / "task-7")
        assert merges[1][0] == ("merge", "--abort")
        assert all(cwd != tmp_path for _, cwd in merges), "Merge in main trotz Konflikt"

    def test_falscher_zustand_lehnt_ab(self, monkeypatch, welt, tmp_path):
        monkeypatch.setattr(f.queue, "hole", lambda tid: {"id": tid, "state": m.PARKED})
        _git(monkeypatch, {})
        assert f.freigeben(7, repo=tmp_path) == "abgelehnt"


class TestAblehnenUndNeuEinreihen:
    def test_ablehnen_parkt_mit_grund(self, monkeypatch, welt):
        assert f.ablehnen(7, "zu gross") is True
        assert welt["parks"] == [(7, "Abgelehnt: zu gross")]

    def test_neu_einreihen_ruft_requeue(self, monkeypatch, welt):
        gerufen = []
        monkeypatch.setattr(f.queue, "requeue", lambda tid: gerufen.append(tid) or True)
        assert f.neu_einreihen(7) is True
        assert gerufen == [7]


def _sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


class TestFreigebenGegenEchtesGit:
    def test_merged_den_branch_wirklich(self, monkeypatch, welt, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _sh("git", "init", "-q", "-b", "main", cwd=repo)
        _sh("git", "config", "user.email", "t@t", cwd=repo)
        _sh("git", "config", "user.name", "t", cwd=repo)
        (repo / "a.txt").write_text("a\n")
        _sh("git", "add", ".", cwd=repo)
        _sh("git", "commit", "-q", "-m", "init", cwd=repo)
        baum = tmp_path / "task-7"
        _sh("git", "worktree", "add", "-b", "forge/task-7", str(baum), "main", cwd=repo)
        (baum / "docs.md").write_text("neu\n")
        _sh("git", "add", ".", cwd=baum)
        _sh("git", "commit", "-q", "-m", "task", cwd=baum)
        # worktree.remove echt laufen lassen, gegen dieses Repo
        from forge import worktree as wt
        monkeypatch.setattr(f.worktree, "remove", lambda tid, repo=None: wt.remove(tid, repo=repo))
        monkeypatch.setattr(wt, "path_for", lambda tid: baum)

        assert f.freigeben(7, repo=repo) == "gemerged"
        assert (repo / "docs.md").read_text() == "neu\n"
        assert not baum.exists()
        zweige = _sh("git", "branch", "--list", "forge/task-7", cwd=repo).stdout.strip()
        assert zweige == ""
```

`tests/test_forge_bericht.py` (neu):

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import bericht as b
from forge import models as m


def _welt(monkeypatch, freigabe=(), geparkt=(), stand=()):
    def _nach_zustand(state, quelle=None):
        return {m.AWAITING_APPROVAL: list(freigabe), m.PARKED: list(geparkt)}.get(state, [])
    monkeypatch.setattr(b.queue, "nach_zustand", _nach_zustand)
    monkeypatch.setattr(b.budget, "stand", lambda: list(stand))


class TestMorgenbericht:
    def test_leere_nacht(self, monkeypatch):
        _welt(monkeypatch)
        text = b.morgenbericht()
        assert "Zur Freigabe: keine" in text
        assert "Geparkt: keine" in text

    def test_nennt_freigaben_mit_id_und_titel(self, monkeypatch):
        _welt(monkeypatch, freigabe=[{"id": 7, "title": "Docs für X", "branch": "forge/task-7"}])
        text = b.morgenbericht()
        assert "#7 Docs für X" in text
        assert "forge.cli approve 7" in text

    def test_nennt_park_gruende(self, monkeypatch):
        _welt(monkeypatch, geparkt=[{"id": 8, "title": "Y", "parked_reason": "Gate rot: tests"}])
        assert "#8 Y — Gate rot: tests" in b.morgenbericht()

    def test_nennt_leere_anbieter_und_verbrauch(self, monkeypatch):
        _welt(monkeypatch, stand=[
            {"provider": "nvidia", "laeufe": 12, "tokens_in": 1000, "tokens_out": 200,
             "erschoepft_seit": "2026-09-15 02:10", "grund": "429"},
            {"provider": "antigravity", "laeufe": 3, "tokens_in": 0, "tokens_out": 0,
             "erschoepft_seit": None, "grund": None},
        ])
        text = b.morgenbericht()
        assert "nvidia: leer seit 2026-09-15 02:10 (429), 12 Läufe" in text
        assert "antigravity: 3 Läufe" in text
```

`tests/test_forge_cli.py` (neu):

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import cli


def _stumm(monkeypatch):
    monkeypatch.setattr(cli.db, "init_pool", lambda *a, **k: None)


class TestCli:
    def test_status_druckt_den_morgenbericht(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.bericht, "morgenbericht", lambda: "BERICHT")
        assert cli.main(["status"]) == 0
        assert "BERICHT" in capsys.readouterr().out

    def test_approve_ruft_freigeben_und_meldet(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        gerufen = []
        monkeypatch.setattr(cli.freigabe, "freigeben", lambda tid: gerufen.append(tid) or "gemerged")
        assert cli.main(["approve", "7"]) == 0
        assert gerufen == [7]
        assert "gemerged" in capsys.readouterr().out

    def test_approve_konflikt_ist_exit_1(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe, "freigeben", lambda tid: "konflikt")
        assert cli.main(["approve", "7"]) == 1

    def test_reject_braucht_grund(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        gerufen = []
        monkeypatch.setattr(cli.freigabe, "ablehnen", lambda tid, grund: gerufen.append((tid, grund)) or True)
        assert cli.main(["reject", "7", "zu gross"]) == 0
        assert gerufen == [(7, "zu gross")]

    def test_requeue(self, monkeypatch):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe, "neu_einreihen", lambda tid: True)
        assert cli.main(["requeue", "7"]) == 0

    def test_stop_schreibt_die_halt_datei(self, monkeypatch, tmp_path):
        _stumm(monkeypatch)
        halt = tmp_path / "halt"
        monkeypatch.setattr(cli.daemon, "HALT_FILE", halt)
        assert cli.main(["stop"]) == 0
        assert halt.exists()
```

- [ ] **Schritt 2: Tests laufen lassen und Fehlschlag prüfen**

Run: `python3.14 -m pytest tests/test_forge_queue.py tests/test_forge_freigabe.py tests/test_forge_bericht.py tests/test_forge_cli.py -q`
Erwartet: FAIL — `ModuleNotFoundError: No module named 'forge.freigabe'` (und
`bericht`, `cli`), `AttributeError: module 'forge.queue' has no attribute 'hole'`.

- [ ] **Schritt 3: Implementierung**

`forge/queue.py`, anhängen:

```python
def hole(task_id: int) -> dict | None:
    """Eine Zeile, oder None."""
    return db.query_one("SELECT * FROM forge_tasks WHERE id=%s", (task_id,))


def nach_zustand(state: str, quelle: str | None = None) -> list[dict]:
    """Alle Tasks in einem Zustand, älteste zuerst. `quelle` siehe TEST_QUELLE."""
    filter_sql, filter_params = _quellen_filter(quelle)
    return db.query(
        f"SELECT * FROM forge_tasks WHERE state=%s {filter_sql} ORDER BY updated_at ASC",
        (state, *filter_params),
    )


def requeue(task_id: int) -> bool:
    """Reiht einen geparkten oder gescheiterten Task neu ein (Nachtrag 2c).

    Setzt die Zähler zurück: `attempts` (Fehlschlag-Spirale) und `refusals`
    (Fix-Runden) — sonst parkt der Task beim ersten Fix sofort wieder mit
    'Fix-Runden-Grenze erreicht'. Der Worktree bleibt stehen; die Arbeit darin
    ist der Grund, warum der Task erneut laufen soll. Ein dort liegendes altes
    Review-Urteil räumt die Implement-Stufe weg (forge/pipeline.py).
    """
    task = hole(task_id)
    if task is None or not m.can_transition(task["state"], m.QUEUED):
        return False
    betroffen = db.execute(
        "UPDATE forge_tasks SET state=%s, attempts=0, refusals=0, parked_reason=NULL, "
        "updated_at=NOW() WHERE id=%s AND state=%s",
        (m.QUEUED, task_id, task["state"]),
    )
    return betroffen == 1
```

`forge/freigabe.py` (neu):

```python
"""Freigabe am Morgen: von AWAITING_APPROVAL nach MERGED — oder zurück zu Timo.

Die Logik liegt hier, nicht im CLI, damit Plan 3 sie aus dem Telegram-Bot
aufrufen kann, ohne sie zu kopieren. Kein Aufruf hier rät: ein schmutziges
main, ein Hauptrepo auf einem anderen Branch oder ein Konflikt beim Nachziehen
enden mit einer Meldung, nicht mit einem halben Merge.
"""
import logging
from pathlib import Path

from forge import MANTIS_REPO, gitctl, journal, queue, worktree
from forge import models as m
from forge.pipeline import BASIS_BRANCH

log = logging.getLogger(__name__)


def _ausgabe(ergebnis) -> str:
    return (ergebnis.stdout or "").strip()


def freigeben(task_id: int, repo: Path | None = None) -> str:
    """Merged den Branch des Tasks nach main.

    Rückgabe:
      "gemerged"  — MERGED, Worktree und Branch entfernt
      "konflikt"  — main hat sich bewegt und der Branch lässt sich nicht
                    konfliktfrei nachziehen, oder der Merge selbst scheitert;
                    Task ist PARKED mit Hinweis
      "abgelehnt" — nichts passiert: falscher Zustand, schmutziges main,
                    Hauptrepo nicht auf main. Der Aufrufer sagt Timo warum
                    (siehe log).
    """
    task = queue.hole(task_id)
    if task is None or task["state"] != m.AWAITING_APPROVAL:
        log.warning(f"Forge-Freigabe: Task {task_id} steht nicht in {m.AWAITING_APPROVAL}")
        return "abgelehnt"

    quelle = Path(repo) if repo is not None else MANTIS_REPO
    zweig = worktree.branch_for(task_id)
    baum = worktree.path_for(task_id)

    # 1. Das Hauptrepo muss auf main stehen und sauber sein. Wir wechseln
    #    keine Branches unter Timo weg.
    kopf = _ausgabe(gitctl.run("rev-parse", "--abbrev-ref", "HEAD", cwd=quelle))
    if kopf != BASIS_BRANCH:
        log.warning(f"Forge-Freigabe: Hauptrepo steht auf '{kopf}', nicht auf '{BASIS_BRANCH}'")
        return "abgelehnt"
    status = gitctl.run("status", "--porcelain", cwd=quelle)
    if status.returncode != 0 or _ausgabe(status):
        log.warning("Forge-Freigabe: main ist nicht sauber — erst committen oder stashen")
        return "abgelehnt"

    # 2. Hat main sich seit Anlage des Worktrees bewegt? Dann den Branch im
    #    Worktree nachziehen; ein Konflikt geht mit Hinweis an Timo zurück.
    basis = _ausgabe(gitctl.run("merge-base", BASIS_BRANCH, zweig, cwd=quelle))
    spitze = _ausgabe(gitctl.run("rev-parse", BASIS_BRANCH, cwd=quelle))
    if basis != spitze:
        nachziehen = gitctl.run("merge", "--no-edit", BASIS_BRANCH, cwd=baum)
        if nachziehen.returncode != 0:
            gitctl.run("merge", "--abort", cwd=baum)
            grund = (f"main hat sich bewegt und {zweig} lässt sich nicht konfliktfrei nachziehen: "
                     f"{(nachziehen.stderr or '').strip()[:300]}")
            queue.park(task_id, current=m.AWAITING_APPROVAL, reason=grund)
            journal.log(task_id, "parked", grund)
            return "konflikt"

    # 3. Der Merge selbst, mit Merge-Commit — im Log soll sichtbar bleiben,
    #    was die Forge als Einheit geliefert hat.
    merge = gitctl.run("merge", "--no-ff", "--no-edit", zweig, cwd=quelle)
    if merge.returncode != 0:
        gitctl.run("merge", "--abort", cwd=quelle)
        grund = f"Merge von {zweig} nach main fehlgeschlagen: {(merge.stderr or '').strip()[:300]}"
        queue.park(task_id, current=m.AWAITING_APPROVAL, reason=grund)
        journal.log(task_id, "parked", grund)
        return "konflikt"

    if not queue.set_state(task_id, m.MERGED, current=m.AWAITING_APPROVAL):
        # Gemerged ist gemerged — den Zustand nicht zurückdrehen, aber laut sein.
        log.error(f"Forge-Freigabe: Task {task_id} ist gemerged, aber der Zustandswechsel schlug fehl")
    journal.log(task_id, "merged", f"{zweig} nach {BASIS_BRANCH} gemerged (Freigabe)")
    worktree.remove(task_id, repo=quelle)
    gitctl.run("branch", "-d", zweig, cwd=quelle)
    return "gemerged"


def ablehnen(task_id: int, grund: str) -> bool:
    """AWAITING_APPROVAL -> PARKED mit Timos Grund."""
    ok = queue.park(task_id, current=m.AWAITING_APPROVAL, reason=f"Abgelehnt: {grund}")
    if ok:
        journal.log(task_id, "parked", f"Abgelehnt: {grund}")
    return ok


def neu_einreihen(task_id: int) -> bool:
    """PARKED/FAILED -> QUEUED, Zähler zurück (siehe queue.requeue)."""
    ok = queue.requeue(task_id)
    if ok:
        journal.log(task_id, "resumed", "Neu eingereiht (forge.cli requeue)")
    return ok
```

`forge/bericht.py` (neu):

```python
"""Der Morgenbericht — was die Nacht gebracht hat, in einem Text.

`forge.cli status` druckt ihn; Plan 3 schickt denselben Text per Telegram.
Deshalb reiner Text, keine Tabellen, keine Farben.
"""
from forge import budget, queue
from forge import models as m


def _zeile_freigabe(t: dict) -> str:
    return f"  #{t['id']} {t['title']}  →  python3.14 -m forge.cli approve {t['id']}"


def _zeile_geparkt(t: dict) -> str:
    return f"  #{t['id']} {t['title']} — {t.get('parked_reason') or 'ohne Grund'}"


def _zeile_anbieter(z: dict) -> str:
    if z.get("erschoepft_seit"):
        return (f"  {z['provider']}: leer seit {z['erschoepft_seit']} ({z.get('grund') or '?'}), "
                f"{z['laeufe']} Läufe")
    return f"  {z['provider']}: {z['laeufe']} Läufe"


def morgenbericht() -> str:
    freigabe = queue.nach_zustand(m.AWAITING_APPROVAL)
    geparkt = queue.nach_zustand(m.PARKED)
    stand = budget.stand()

    teile = ["Forge — Morgenbericht", ""]
    teile.append("Zur Freigabe: " + (f"{len(freigabe)}" if freigabe else "keine"))
    teile += [_zeile_freigabe(t) for t in freigabe]
    teile.append("")
    teile.append("Geparkt: " + (f"{len(geparkt)}" if geparkt else "keine"))
    teile += [_zeile_geparkt(t) for t in geparkt]
    teile.append("")
    teile.append("Anbieter:" if stand else "Anbieter: keine Läufe diese Nacht")
    teile += [_zeile_anbieter(z) for z in stand]
    return "\n".join(teile).rstrip() + "\n"
```

`forge/cli.py` (neu):

```python
"""Timos Griff an die Forge, bis Plan 3 den Telegram-Bot bringt.

    python3.14 -m forge.cli status
    python3.14 -m forge.cli approve <id>
    python3.14 -m forge.cli reject <id> "<grund>"
    python3.14 -m forge.cli requeue <id>
    python3.14 -m forge.cli stop

Dünne Hülle: die Logik liegt in forge/freigabe.py und forge/bericht.py.
"""
import argparse
import sys

from core import db

from forge import bericht, daemon, freigabe


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge.cli", description="Forge-Freigabe und -Status")
    sub = p.add_subparsers(dest="befehl", required=True)
    sub.add_parser("status", help="Morgenbericht drucken")
    a = sub.add_parser("approve", help="Task mergen (aus awaiting_approval)")
    a.add_argument("task_id", type=int)
    r = sub.add_parser("reject", help="Task ablehnen und parken")
    r.add_argument("task_id", type=int)
    r.add_argument("grund")
    q = sub.add_parser("requeue", help="geparkten/gescheiterten Task neu einreihen")
    q.add_argument("task_id", type=int)
    sub.add_parser("stop", help="weicher Stop: laufende Stufe endet, dann der Daemon")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.befehl == "stop":
        daemon.HALT_FILE.write_text("stop\n")
        print(f"Halt angefordert ({daemon.HALT_FILE}) — der Daemon beendet sich nach dem laufenden Tick.")
        return 0

    db.init_pool()
    if args.befehl == "status":
        print(bericht.morgenbericht(), end="")
        return 0
    if args.befehl == "approve":
        ergebnis = freigabe.freigeben(args.task_id)
        print(f"Task {args.task_id}: {ergebnis}")
        return 0 if ergebnis == "gemerged" else 1
    if args.befehl == "reject":
        ok = freigabe.ablehnen(args.task_id, args.grund)
        print(f"Task {args.task_id}: {'geparkt' if ok else 'nicht in awaiting_approval'}")
        return 0 if ok else 1
    if args.befehl == "requeue":
        ok = freigabe.neu_einreihen(args.task_id)
        print(f"Task {args.task_id}: {'neu eingereiht' if ok else 'nicht geparkt/gescheitert'}")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

`"resumed"` und `"parked"` und `"merged"` sind bereits in `journal.KINDS`.

- [ ] **Schritt 4: Tests laufen lassen und Erfolg prüfen**

Run: `python3.14 -m pytest tests/test_forge_queue.py tests/test_forge_freigabe.py tests/test_forge_bericht.py tests/test_forge_cli.py -q`
Erwartet: PASS.

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add forge/queue.py forge/freigabe.py forge/bericht.py forge/cli.py tests/test_forge_queue.py tests/test_forge_freigabe.py tests/test_forge_bericht.py tests/test_forge_cli.py
git commit -m "feat(forge): Freigabe, Morgenbericht und CLI fuer die Nacht ohne Bot

python3.14 -m forge.cli status|approve|reject|requeue|stop. Die Logik
liegt in forge/freigabe.py und forge/bericht.py, damit Plan 3 sie aus dem
Telegram-Bot aufruft. approve merged nur, wenn main sauber ist und das
Hauptrepo auf main steht; ein bewegtes main wird im Worktree nachgezogen,
ein Konflikt parkt mit Hinweis. requeue setzt attempts und refusals
zurueck."
```

---

### Task 6: Der Nachtlauf-Simulator

Der eigentliche Ertrag. Drei Pläne in Folge haben Task-Reviews grün gesehen,
was das Abschluss-Review als Critical fand — weil kein Test je das echte
`daemon.main()` über mehrere Ticks gegen die echte Datenbank hat laufen lassen.
Dieser Test tut das, mit Störungen, und prüft Invarianten statt Einzelzustände.

**Files:**
- Create: `tests/test_forge_nachtlauf.py`

**Interfaces:**
- Consumes: `daemon.main`, `daemon.tick`, `daemon.im_nachtfenster`,
  `daemon.HALT_FILE`, `daemon.STOP_FILE`, `daemon.KONTINGENT_SLEEP_SECONDS`;
  `queue.claim_next(quelle)`, `queue.TEST_QUELLE`, `queue.enqueue`;
  `ketten.KETTEN`, `budget.provider_von_modell`; `gate.GateErgebnis`;
  `pipeline.MAX_FIXRUNDEN`; `stages.VERDIKT_DATEI`; `models.*`
- Produces: keine

- [ ] **Schritt 1: Den Test schreiben**

```python
"""Der Nachtlauf-Simulator: das ECHTE daemon.main() über viele Ticks gegen die
ECHTE Datenbank, mit Störungen nach Drehbuch.

Warum: Drei Pläne in Folge (1, 2a, 2b) haben Task-Reviews grün gesehen, was
das Abschluss-Review als Critical fand — Rate-Limit-Arithmetik über Ticks,
veraltete Artefakte nach einem Absturz, Runden, die ohne Lauf verbraucht
wurden. Kein Unit-Test sieht so etwas, weil keiner mehr als einen Tick
kennt. Dieser Test kennt die ganze Nacht.

Gemockt sind nur die Ränder: worktree.create (-> tmp_path), gate.pruefe,
time.sleep (zählt statt zu warten), die Fenster-Uhr und die LLM-Backends —
durch ein Drehbuch, das je Aufruf sagt, was passiert. Alles dazwischen
(queue, set_state-CAS, _waehle_stufe, Kettenwahl, Rundenzählung, die
Sleep-Kette in main) ist echt.

Die Kettenwahl ist echt, nur budget.ist_erschoepft ist ein Set: so bleibt
die Anbieter-Arithmetik (Erschöpfung gilt anbieterweit) im Test dieselbe wie
in der Nacht.
"""
import functools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import db
from forge import budget, daemon as d, gate, ketten, models as m, pipeline as pl, queue, stages
from forge.runner import RunResult


def _db_erreichbar() -> bool:
    try:
        db.init_pool()
        db.query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_erreichbar(), reason="PostgreSQL nicht erreichbar — Nachtlauf übersprungen")


class _NachtEnde(BaseException):
    """Hält main() nach N Ticks an. BaseException, damit kein except Exception
    im Daemon sie schluckt."""


class _Absturz(BaseException):
    """Der Prozess stirbt an genau dieser Zeile. Wie _NachtEnde, aber der
    Test startet main() danach erneut — das ist der Neustart."""


class Drehbuch:
    """Antworten je Backend-Aufruf, in Reihenfolge. Ein Eintrag ist ein String
    ('ok', 'rate_limit', 'fehler', 'verdikt_pass', 'verdikt_fail') oder eine
    Funktion (agent, cwd) -> RunResult. Ist das Drehbuch leer, kommt 'ok'."""

    def __init__(self, *schritte):
        self.schritte = list(schritte)
        self.aufrufe: list[str] = []

    def __call__(self, prompt, cwd, timeout, agent, model):
        self.aufrufe.append(agent)
        schritt = self.schritte.pop(0) if self.schritte else "ok"
        if callable(schritt):
            return schritt(agent, Path(cwd))
        if schritt == "rate_limit":
            return RunResult(ok=False, rate_limited=True, error="429")
        if schritt == "fehler":
            return RunResult(ok=False, error="kaputt")
        if schritt in ("verdikt_pass", "verdikt_fail"):
            assert agent == "review", f"Verdikt-Schritt, aber Stufe ist '{agent}'"
            ok = schritt == "verdikt_pass"
            datei = Path(cwd) / stages.VERDIKT_DATEI
            datei.parent.mkdir(parents=True, exist_ok=True)
            datei.write_text(json.dumps({
                "verdict": "pass" if ok else "fail",
                "findings": [] if ok else [{"severity": "critical", "what": "x"}]}))
            return RunResult(ok=True, text="egal")
        return RunResult(ok=True, text="egal")


@pytest.fixture
def nacht(monkeypatch, tmp_path):
    """Eine leere Nacht: Testtasks weg, Ränder gemockt, Zähler bei null.

    Liefert ein Objekt mit `.tasks(n)`, `.drehbuch`, `.erschoepft`,
    `.laufen(max_ticks)`, `.schlaefe`, `.zustand(id)`.
    """
    db.execute("DELETE FROM forge_tasks WHERE source=%s", (queue.TEST_QUELLE,))

    class _Nacht:
        pass

    n = _Nacht()
    n.ids = []
    n.drehbuch = Drehbuch()
    n.erschoepft: set[str] = set()
    n.schlaefe: list[int] = []
    n.ticks = 0
    n.fenster = [True]  # letzter Wert gilt weiter

    def tasks(anzahl):
        for i in range(anzahl):
            n.ids.append(queue.enqueue(f"Nachtlauf-Test {i}", "wegwerf",
                                       source=queue.TEST_QUELLE, priority=100 - i))
        return list(n.ids)
    n.tasks = tasks

    def zustand(task_id):
        return db.query_one("SELECT state, refusals FROM forge_tasks WHERE id=%s", (task_id,))
    n.zustand = zustand

    def baum(task_id):
        return tmp_path / f"task-{task_id}"
    n.baum = baum

    # Ränder
    monkeypatch.setattr(d.db, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(d.db, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(d, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(d, "HALT_FILE", tmp_path / "halt")
    monkeypatch.setattr(d, "im_nachtfenster",
                        lambda jetzt=None: n.fenster.pop(0) if len(n.fenster) > 1 else n.fenster[0])
    monkeypatch.setattr(d.queue, "claim_next", functools.partial(queue.claim_next, quelle=queue.TEST_QUELLE))
    monkeypatch.setattr(d.worktree, "create",
                        lambda task_id: (baum(task_id).mkdir(parents=True, exist_ok=True) or baum(task_id)))
    monkeypatch.setattr(d.gate, "pruefe", lambda baum, basis="main": gate.GateErgebnis(ok=True))
    monkeypatch.setattr(d.time, "sleep", lambda s: n.schlaefe.append(s))
    # Journalzeilen ohne task_id (daemon_start/-stop) landen sonst als Waisen
    # in der Produktionstabelle — forge_journal.task_id NULL hat keinen CASCADE.
    echtes_log = d.journal.log
    monkeypatch.setattr(d.journal, "log",
                        lambda tid, *a, **k: echtes_log(tid, *a, **k) if tid is not None else None)

    monkeypatch.setattr(pl.backends, "hole", lambda name: n.drehbuch)
    monkeypatch.setattr(pl, "_artefakt_vorhanden", lambda *a: True)
    monkeypatch.setattr(pl, "_schreibe_diff", lambda w: None)
    monkeypatch.setattr(pl, "_committe_stufenarbeit", lambda *a: None)
    monkeypatch.setattr(pl, "_committe_artefakt", lambda *a: None)
    monkeypatch.setattr(pl.budget, "buche", lambda model, tokens_in, tokens_out: None)
    monkeypatch.setattr(pl.budget, "markiere_erschoepft",
                        lambda model, grund: n.erschoepft.add(budget.provider_von_modell(model)))
    # Kettenwahl ECHT — nur die Erschöpfungsfrage kommt aus dem Set.
    monkeypatch.setattr(ketten.budget, "ist_erschoepft",
                        lambda model: budget.provider_von_modell(model) in n.erschoepft)

    echter_tick = d.tick

    def _tick():
        n.ticks += 1
        if n.ticks > n.max_ticks:
            raise _NachtEnde
        return echter_tick()
    monkeypatch.setattr(d, "tick", _tick)

    def laufen(max_ticks):
        """main() bis max_ticks oder bis es von selbst endet. Rückgabe:
        'nacht_ende' (Tick-Limit), 'beendet' (main kehrte zurück),
        'absturz' (ein Absturz-Schritt hat den Prozess getötet)."""
        n.max_ticks = n.ticks + max_ticks
        try:
            d.main()
            return "beendet"
        except _NachtEnde:
            return "nacht_ende"
        except _Absturz:
            return "absturz"
    n.laufen = laufen

    yield n

    db.execute("DELETE FROM forge_tasks WHERE source=%s", (queue.TEST_QUELLE,))


def _invarianten(n):
    """Was nach JEDER Nacht gelten muss, egal was das Drehbuch tat."""
    assert not d.STOP_FILE.exists(), "Not-Aus-Datei geschrieben"
    for tid in n.ids:
        z = n.zustand(tid)
        assert z["refusals"] <= pl.MAX_FIXRUNDEN, f"Task {tid}: {z['refusals']} Fix-Runden"
        assert z["state"] in m._TRANSITIONS, f"Task {tid}: unbekannter Zustand {z['state']}"
        verdikt = n.baum(tid) / stages.VERDIKT_DATEI
        if z["state"] == m.REVIEWING and verdikt.is_file():
            # Ein Urteil in REVIEWING muss aus einem Review-Lauf stammen, der
            # NACH dem letzten Implement/Fix lag — sonst ist es veraltet.
            letzte_arbeit = max((k for k, a in enumerate(n.drehbuch.aufrufe) if a in ("implement", "fix")), default=-1)
            letztes_review = max((k for k, a in enumerate(n.drehbuch.aufrufe) if a == "review"), default=-1)
            assert letztes_review > letzte_arbeit, f"Task {tid}: veraltetes Urteil in REVIEWING"


class TestGuteNacht:
    def test_zwei_tasks_landen_zur_freigabe(self, nacht):
        a, b = nacht.tasks(2)
        nacht.drehbuch = Drehbuch(*(["ok", "ok", "ok", "verdikt_pass"] * 2))
        ergebnis = nacht.laufen(max_ticks=20)
        assert ergebnis == "nacht_ende"
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.zustand(b)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review"] * 2
        _invarianten(nacht)
```

Die Fixture-Lambda `lambda name: n.drehbuch` liest das Attribut bei jedem
Aufruf — ein Test darf `nacht.drehbuch` einfach neu zuweisen.

Die weiteren Szenarien (gleiche Datei):

```python
class TestFixSchleifeInDerNacht:
    def test_negatives_review_wird_gefixt_und_dann_freigegeben(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=12)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review", "fix", "review"], \
            "nach dem Fix muss direkt ein Review folgen — kein zweites Implement"
        assert nacht.zustand(a)["refusals"] == 1
        _invarianten(nacht)

    def test_zwei_negative_reviews_parken_ohne_dritten_fix(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_fail", "ok", "verdikt_fail")
        nacht.laufen(max_ticks=15)
        z = nacht.zustand(a)
        assert z["state"] == m.PARKED
        assert z["refusals"] == pl.MAX_FIXRUNDEN
        assert nacht.drehbuch.aufrufe.count("fix") == pl.MAX_FIXRUNDEN
        _invarianten(nacht)

    def test_rate_limit_im_fix_verbraucht_keine_runde(self, nacht):
        """Fund I4 (2b): vorher parkte der Task nach zwei Rate-Limits im Fix
        mit 'Fix-Runden-Grenze erreicht', ohne dass je ein Fix lief."""
        (a,) = nacht.tasks(1)
        # implement-Kette: minimax (nvidia) -> nemotron (nvidia) -> kimi (nvidia) -> gemini (google).
        # Ein nvidia-Rate-Limit nimmt drei Glieder auf einmal; dann bleibt gemini.
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "rate_limit", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=15)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.zustand(a)["refusals"] == 1
        assert nacht.drehbuch.aufrufe.count("fix") == 2
        _invarianten(nacht)


class TestKontingentInDerNacht:
    def test_rate_limit_kaskade_legt_die_forge_nicht_still(self, nacht):
        """Fund I1 (2b): zwei Rate-Limits nacheinander leeren beide Anbieter
        der Implement-Kette. Erwartet: 'kontingent', Zustand steht, keine
        Not-Aus-Datei — und nach dem Budget-Reset geht es weiter."""
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "rate_limit", "rate_limit", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=6)
        assert nacht.zustand(a)["state"] == m.IMPLEMENTING, "Kontingent hat den Zustand verbrannt"
        assert d.KONTINGENT_SLEEP_SECONDS in nacht.schlaefe
        assert not d.STOP_FILE.exists()
        assert nacht.erschoepft == {"nvidia", "google"}
        # Nächste Nacht: Budget zurück
        nacht.erschoepft.clear()
        nacht.laufen(max_ticks=6)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        _invarianten(nacht)

    def test_drei_kontingent_ticks_schreiben_keine_not_aus_datei(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.erschoepft.update({"nvidia", "google"})
        nacht.laufen(max_ticks=5)
        assert not d.STOP_FILE.exists()
        assert nacht.zustand(a)["state"] == m.SPECCING
        assert nacht.schlaefe.count(d.KONTINGENT_SLEEP_SECONDS) >= 3
        _invarianten(nacht)


class TestAbsturzInDerNacht:
    def test_absturz_nach_dem_fix_vor_der_zaehlung_fuehrt_zum_review(self, nacht, monkeypatch):
        """Fund I3 (2b), am Fix: stirbt der Prozess, nachdem das alte Urteil
        verworfen ist, darf der nächste Tick NICHT erneut fixen."""
        (a,) = nacht.tasks(1)
        echtes_zaehlen = pl.queue.zaehle_fixrunde
        einmal = {"gestorben": False}

        def _zaehlen_dann_sterben(task_id):
            if not einmal["gestorben"]:
                einmal["gestorben"] = True
                raise _Absturz
            return echtes_zaehlen(task_id)
        monkeypatch.setattr(pl.queue, "zaehle_fixrunde", _zaehlen_dann_sterben)

        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_fail", "ok", "verdikt_pass")
        assert nacht.laufen(max_ticks=10) == "absturz"
        # Neustart
        nacht.laufen(max_ticks=10)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        assert nacht.drehbuch.aufrufe == ["spec", "plan", "implement", "review", "fix", "review"]
        _invarianten(nacht)

    def test_absturz_mitten_in_einer_stufe_parkt_und_die_nacht_geht_weiter(self, nacht):
        """Der bestehende Vertrag von tick(): eine Ausnahme aus einem Lauf
        parkt den Task. Der zweite Task kommt trotzdem dran."""
        a, b = nacht.tasks(2)

        def _explodiert(agent, cwd):
            raise RuntimeError("Backend kaputt")
        nacht.drehbuch = Drehbuch("ok", _explodiert, "ok", "ok", "ok", "verdikt_pass")
        nacht.laufen(max_ticks=12)
        assert nacht.zustand(a)["state"] == m.PARKED
        assert nacht.zustand(b)["state"] == m.AWAITING_APPROVAL
        assert not d.STOP_FILE.exists()
        _invarianten(nacht)


class TestFensterende:
    def test_fensterende_laesst_den_task_aktiv_und_die_naechste_nacht_setzt_auf(self, nacht):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_pass")
        nacht.fenster = [True, True, False]  # zwei Ticks, dann 07:00
        assert nacht.laufen(max_ticks=10) == "beendet"
        assert nacht.zustand(a)["state"] == m.IMPLEMENTING
        assert nacht.ticks == 2
        nacht.fenster = [True]
        nacht.laufen(max_ticks=10)
        assert nacht.zustand(a)["state"] == m.AWAITING_APPROVAL
        _invarianten(nacht)

    def test_weicher_stop_beendet_nach_dem_laufenden_tick(self, nacht, monkeypatch):
        (a,) = nacht.tasks(1)
        nacht.drehbuch = Drehbuch("ok", "ok", "ok", "verdikt_pass")
        gesehen = []
        zaehlender_tick = d.tick  # der Zähl-Wrapper der Fixture

        def _tick_dann_halt():
            r = zaehlender_tick()
            gesehen.append(r)
            d.HALT_FILE.write_text("stop")
            return r
        monkeypatch.setattr(d, "tick", _tick_dann_halt)

        assert nacht.laufen(max_ticks=10) == "beendet"
        assert gesehen == ["weiter"]
        assert nacht.zustand(a)["state"] == m.PLANNING
        assert not d.HALT_FILE.exists()
        _invarianten(nacht)
```

- [ ] **Schritt 2: Test laufen lassen**

Run: `python3.14 -m pytest tests/test_forge_nachtlauf.py -q`
Erwartet: PASS, wenn Tasks 1–5 stehen. Jeder Fehlschlag ist entweder ein
Fehler im Drehbuch/der Fixture — oder ein echter Befund über die Nacht. Vor
dem Anpassen eines Tests im Report begründen, welches von beiden.

Bekannte Feinheit: `_invarianten` prüft die Urteils-Frische über die
Drehbuch-Aufrufreihenfolge, nicht je Task. Bei zwei Tasks in einer Nacht ist
das nur dann scharf, wenn beide vor der Prüfung fertig sind — die Szenarien
oben erfüllen das.

- [ ] **Schritt 3: Mutationsnachweis gegen die vier Importants aus 2b**

Je einzeln zurückdrehen, `python3.14 -m pytest tests/test_forge_nachtlauf.py -q`
laufen lassen, genannten Test rot sehen, `git checkout -- forge/` und
`git status --short` (nur die neue Testdatei):

1. **I1** — in `forge/pipeline.py` den Rate-Limit-Zweig auf bedingungsloses
   `return "fehler"` → `test_rate_limit_kaskade_legt_die_forge_nicht_still` rot
   (Not-Aus-Datei oder Zustand).
2. **I2** — im `ist_fix`-Zweig statt `return "weiter"` einen
   `queue.set_state(task_id, m.IMPLEMENTING, current=state)` versuchen →
   `test_negatives_review_wird_gefixt_und_dann_freigegeben` rot (Übergang
   verboten → Park, oder Implement läuft erneut).
3. **I3** — `_verwirf_review_artefakte(worktree)` im `ist_fix`-Zweig entfernen →
   `test_absturz_nach_dem_fix_vor_der_zaehlung_fuehrt_zum_review` rot und
   `test_negatives_review_wird_gefixt_und_dann_freigegeben` rot (zweiter Fix
   statt Review).
4. **I4** — `queue.zaehle_fixrunde` wieder vor den Lauf →
   `test_rate_limit_im_fix_verbraucht_keine_runde` rot.

Alle vier Ausgaben in den Report. Bleibt einer grün, ist das Szenario zu
schwach und wird verschärft, bevor committet wird.

- [ ] **Schritt 4: Aufräumen belegen**

```bash
python3.14 -c "
from core import db
db.init_pool()
print('Testtasks uebrig:', db.query(\"SELECT count(*) AS n FROM forge_tasks WHERE source='test'\")[0]['n'])
print('Journal-Waisen:', db.query('SELECT count(*) AS n FROM forge_journal WHERE task_id IS NULL AND message LIKE %s', ('%Nachtfenster%',))[0]['n'])
"
```
Erwartet: beide `0`.

- [ ] **Schritt 5: Volle Suite und ruff**

Run: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`

- [ ] **Schritt 6: Committen**

```bash
git add tests/test_forge_nachtlauf.py
git commit -m "test(forge): Nachtlauf-Simulator — echtes main() ueber viele Ticks gegen echte DB

Drehbuch-Backend mit Rate-Limit, Fehler, Verdikt und Absturz; echte
Kettenwahl mit Erschoepfung als Set; Invarianten statt Einzelzustaende.
Die vier Importants aus dem 2b-Abschlussreview (Rate-Limit-Spirale,
Implement-Re-Run, veraltetes Urteil, verbrauchte Runde) machen ihn
einzeln zurueckgedreht rot."
```

---

## Danach

**Timos Schritte ausserhalb des Worktrees** (nicht Teil des Plans, weil sie
das System verändern). Die Reihenfolge ist der Punkt: das lokale `main` liegt
am 2026-09-14 75 Commits hinter `forge/pipeline` und enthält nur die Forge
aus Plan 1 — ein `git checkout main` als erster Schritt würde genau diese
alte Forge unter launchd stellen (Abschluss-Review 2c, C2).

1. `forge/2c-erste-nacht` nach `forge/pipeline` mergen.
2. `main` nachziehen: `git merge --ff-only forge/pipeline` — ff-only, damit
   `main` exakt der geprüfte Stand ist und kein Merge-Commit dazwischenliegt.
3. `git checkout main` in `~/Mantis`.
4. `git status --porcelain --untracked-files=no` muss leer sein (das ist die
   Prüfung, die `freigeben` macht; untracked Dateien stören nicht).
5. plist installieren (drei Zeilen in `forge/launchd/README.md`) **vor
   23:00**. Tagesprobe: `launchctl kickstart -k gui/$(id -u)/com.mantis.forge`
   — das Log (`/tmp/mantis_forge_out.log`) zeigt „Nachtfenster zu Ende", das
   Journal hat ein `daemon_start`- und ein `daemon_stop`-Paar. Damit ist
   bewiesen, dass launchd den richtigen Interpreter, das richtige Repo und
   die DB findet.
6. `python3.14 -m forge.cli status` — druckt den Bericht mit „Daemon:
   gestartet …, beendet …" aus der Tagesprobe. Die alten Parks (Tasks 3–6,
   Worktrees aus dem Vor-2a-`main`) **nicht** requeuen — ihre Worktrees
   basieren auf einem Stand ohne Kettenwahl und Fix-Schleife.
7. Genau **einen** kleinen Task einreihen, der vollständig in den erlaubten
   Zonen (`tests/`, `docs/`, `scripts/`, `tools/`, `core/skills/`) liegt.
8. Mac wach und am Strom; keine `~/.mantis-forge-stop` und keine
   `~/.mantis-forge-halt`.
9. Zwischen 23 und 7 kein manuelles `pytest tests/` — die beiden DB-Tests
   kollidieren mit dem Gate-Lauf auf `source='test'`. Kein `launchctl bootout`
   mitten im Lauf; wenn es sein muss: `python3.14 -m forge.cli stop` (weicher
   Halt nach dem laufenden Tick).
10. Morgens `python3.14 -m forge.cli status`, den Diff des Worktrees prüfen,
    dann `approve <id>` (oder `reject <id> "<grund>"`).

**Plan 2d:** Parallelität, Kurzbahn, Mistral-Commit-Texte. **Plan 3:** Scout
und Telegram-Bot (ruft `freigabe.*` und `bericht.morgenbericht`). **Plan 4:**
Dashboard.
