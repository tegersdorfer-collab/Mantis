# COROS MCP — Phase B: Prosa parsen und importieren

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die Tabelle `health_data` wird aus der COROS-Cloud gefüllt statt aus dem HealthKit-Push.

**Architecture:** `domains/coros/mapping.py` verwandelt die Textantworten des COROS-MCP in Spaltenwerte — reine Funktionen, keine I/O, streng gegen Formatabweichung. `domains/coros/importer.py` ruft die Tools auf, führt die Quellen je Tag zusammen und schreibt über eine neue Funktion `health.upsert_day`. Zuletzt wechselt `dashboard.refresh_health()` die Quelle. Alles darüber — Scores, Dashboard, Voice, Agent-Tools — bleibt unangetastet.

**Tech Stack:** Python 3.14, httpx, pytest. Keine neue Dependency.

**Spec:** [docs/superpowers/specs/2026-08-15-coros-mcp-health-sync-design.md](../specs/2026-08-15-coros-mcp-health-sync-design.md) — **lies zuerst den "Nachtrag 2026-08-16"**, er widerlegt vier Annahmen des übrigen Dokuments.

**Voraussetzung:** Phase A ist auf `coros/mcp-phase-a` fertig (`domains/coros/oauth.py`, `client.py`, Login-Skript, Erkundungs-Skript). Diese Phase baut darauf auf.

## Global Constraints

- **Keine neue Dependency.** Nur `httpx`, `pytest` und Stdlib.
- Kommentare, Logmeldungen und Doku auf Deutsch.
- Tests: pytest, `sys.path`-Insert am Dateikopf wie in `tests/test_health_mapping.py`. Kein echtes Netz — Parser sind rein, der Importer wird gegen einen Fake-Client getestet.
- **Datumsformat der COROS-Tools ist `yyyyMMdd`**, nicht ISO. Die Tagesüberschriften in den Antworten sind teils `20260813`, teils `2026-08-13` — je Tool unterschiedlich, siehe die Fixtures.
- **Alle Antworten sind Text.** `payload()` liefert in dieser Phase immer einen `str`. Kein Tool hat ein `outputSchema`.
- **Strenge vor Vollständigkeit.** Ein fehlender Wert ist harmlos (die Scores melden ehrlich "zu wenig Daten"). Ein falsch geparster Wert sieht aus wie eine Messung und vergiftet die Baselines von `health_scores.compute_baselines`. Im Zweifel nichts schreiben.
- **`payload()` gibt `str` zurück, aber nicht jeder `str` sind Daten.** COROS antwortet auf manche Aufrufe mit `isError: false` und einem Fehlertext im Inhalt. Jeder Parser prüft das zuerst.
- Diese Phase **löscht nichts**. Der Push-Pfad (`POST /api/health/push`, `import_health`, `map_health_fields`, `HEALTH_API_URL`) bleibt vorerst stehen und wird in Phase C abgerissen.

---

### Task 1: Parser-Fundament und Tageswerte

**Files:**
- Create: `domains/coros/mapping.py`
- Create: `tests/fixtures/coros/daily_health.txt`
- Create: `tests/fixtures/coros/error_anomalies.txt`
- Create: `tests/test_coros_mapping.py`

**Interfaces:**
- Consumes: nichts (reine Funktionen)
- Produces:
  - `CorosFormatError(RuntimeError)` — Antwort ist unbrauchbar (Fehlertext oder Struktur unkenntlich)
  - `check_response(text: str) -> str` — wirft `CorosFormatError` bei Server-Fehlertexten, gibt den Text sonst unverändert zurück
  - `parse_int(raw: str) -> int | None` — "12,500" → 12500; "No data" → None
  - `parse_duration_h(raw: str) -> float | None` — "8h 10min" → 8.17; "59 min" → 0.98; "0 min" → 0.0
  - `LIMITS: dict[str, tuple[float, float]]` — Plausibilitätsgrenzen je Spalte
  - `sane(col: str, value)` — Wert oder `None`, wenn außerhalb `LIMITS`
  - `parse_daily_health(text: str) -> dict[str, dict]` — ISO-Datum → Spaltenwerte

Die Fixtures sind **synthetisch**: echtes Format, erfundene Zahlen. Die echten Dumps aus Phase A liegen in `data/coros_samples/` (gitignored) und enthalten Geburtsdatum, Größe, Gewicht und GPS-Startkoordinaten — davon kommt nichts ins Repo. Die Fixtures kodieren zusätzlich absichtlich Randfälle: einen Tag ohne Schlafblock, einen Tag mit `No data`, Tausendertrennzeichen.

- [ ] **Step 1: Fixtures anlegen**

`tests/fixtures/coros/daily_health.txt` — exakt so, inklusive der zwei Leerzeichen Einrückung:

```
Daily Health Data — Last 7 days | Resting HR: 55 bpm | HRV Baseline: 50 ms
Note: sleep entries are dated by their wake-up day.

--- 20260810 ---
Steps: 3,140 | Calories: 500 kcal | Exercise: 0 min
Stress: Avg 30
Sleep Summary:
  Total: 7h 30min | Deep: 1h 30min | Light: 4h 0min | REM: 1h 45min | Awake: 15 min
  Sleep HR: Avg 55 bpm | Min 45 bpm | Max 70 bpm

--- 20260811 ---
Steps: 12,500 | Calories: 1,200 kcal | Exercise: 25 min
Stress: Avg 50
Sleep Summary:
  Total: 6h 0min | Deep: 1h 0min | Light: 3h 30min | REM: 1h 15min | Awake: 15 min
  Sleep HR: Avg 60 bpm | Min 50 bpm | Max 85 bpm

--- 20260812 ---
Steps: 8,000 | Calories: 700 kcal | Exercise: 10 min
Stress: No data

--- 20260813 ---
Steps: 5,000 | Calories: 400 kcal | Exercise: 5 min
Stress: Avg 20
Sleep Summary:
  Total: 8h 0min | Deep: 2h 0min | Light: 4h 30min | REM: 1h 20min | Awake: 10 min
  Sleep HR: Avg 58 bpm | Min 48 bpm | Max 75 bpm
```

**Die Zahlen sind frei erfunden und müssen es bleiben.** Ein erster Entwurf dieses Plans
hatte hier Timos echte Messwerte stehen (nur nach Tagen umsortiert) — das Review hat es
gefangen, 27 von 27 Werten stammten aus dem echten Dump. Wer diese Fixture je erweitert:
nichts aus `data/coros_samples/` abschreiben. Die Datumsangaben bleiben dagegen, wie sie
sind — Task 4 führt diese Fixture über dieselben Tage mit den Schlaf-, HRV- und
Puls-Fixtures zusammen.

`tests/fixtures/coros/error_anomalies.txt` — der echte Fehlertext, den COROS mit `isError: false` liefert:

```
Tool call anomalies detected. High risk of session context pollution or request exceeds the LLM capability boundary. Resolution Strategy: 1. Initialize a new session to reset context; 2. Upgrade or switch to a high-capacity model instance.
```

- [ ] **Step 2: Test-Datei schreiben**

`tests/test_coros_mapping.py`:

```python
"""Tests für den COROS-Prosa-Parser (domains/coros/mapping.py).

Reine Funktionen, keine DB, kein Netz. Die Fixtures unter tests/fixtures/coros/
haben das echte Antwortformat, aber erfundene Zahlen — die echten Dumps liegen
gitignored unter data/coros_samples/ und enthalten personenbezogene Daten.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import mapping

FIX = Path(__file__).parent / "fixtures" / "coros"


def _fix(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── check_response ──────────────────────────────────────────────────────────

def test_server_error_text_raises_even_without_iserror():
    with pytest.raises(mapping.CorosFormatError, match="anomalies"):
        mapping.check_response(_fix("error_anomalies.txt"))


def test_date_format_complaint_raises():
    with pytest.raises(mapping.CorosFormatError):
        mapping.check_response("startDate must be in yyyyMMdd format.")


def test_empty_response_raises():
    with pytest.raises(mapping.CorosFormatError):
        mapping.check_response("   ")


def test_normal_text_passes_through_unchanged():
    text = _fix("daily_health.txt")
    assert mapping.check_response(text) == text


# ── parse_int ───────────────────────────────────────────────────────────────

def test_parse_int_strips_thousands_separator():
    assert mapping.parse_int("12,500") == 12500


def test_parse_int_plain():
    assert mapping.parse_int("692") == 692


def test_parse_int_no_data_is_none():
    assert mapping.parse_int("No data") is None


def test_parse_int_garbage_is_none():
    assert mapping.parse_int("") is None
    assert mapping.parse_int("--") is None


# ── parse_duration_h ────────────────────────────────────────────────────────

def test_duration_hours_and_minutes():
    assert mapping.parse_duration_h("8h 10min") == 8.17


def test_duration_minutes_only():
    assert mapping.parse_duration_h("59 min") == 0.98


def test_duration_zero():
    assert mapping.parse_duration_h("0 min") == 0.0


def test_duration_hours_with_zero_minutes():
    assert mapping.parse_duration_h("2h 0min") == 2.0


def test_duration_no_data_is_none():
    assert mapping.parse_duration_h("No data") is None


# ── sane ────────────────────────────────────────────────────────────────────

def test_sane_passes_plausible_value():
    assert mapping.sane("steps", 9677) == 9677


def test_sane_rejects_impossible_value():
    # Ein Parser-Ausrutscher, der zwei Zahlen verklebt, darf nicht in die DB.
    assert mapping.sane("steps", 46201138) is None


def test_sane_rejects_negative():
    assert mapping.sane("resting_hr", -5) is None


def test_sane_passes_none_through():
    assert mapping.sane("steps", None) is None


def test_sane_unknown_column_passes_through():
    assert mapping.sane("gibt_es_nicht", 42) == 42


# ── parse_daily_health ──────────────────────────────────────────────────────

def test_daily_health_finds_every_day():
    days = mapping.parse_daily_health(_fix("daily_health.txt"))
    assert sorted(days) == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]


def test_daily_health_basic_columns():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-11"]
    assert d["steps"] == 12500
    assert d["active_calories"] == 1200
    assert d["exercise_minutes"] == 25
    assert d["stress_avg"] == 50


def test_daily_health_sleep_stages():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-10"]
    assert d["sleep_duration"] == 7.5
    assert d["sleep_deep"] == 1.5
    assert d["sleep_core"] == 4.0    # "Light" ist Apples "Core"
    assert d["sleep_rem"] == 1.75
    assert d["sleep_awake"] == 0.25


def test_daily_health_day_without_sleep_block_keeps_other_fields():
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-12"]
    assert d["steps"] == 8000
    assert "sleep_duration" not in d


def test_daily_health_no_data_field_is_omitted_not_zero():
    # "Stress: No data" darf nicht als 0 in der DB landen — 0 wäre eine Aussage.
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-12"]
    assert "stress_avg" not in d


def test_daily_health_does_not_map_sleep_hr_to_daily_hr():
    # "Sleep HR" ist Schlaf-Puls, nicht Tages-Puls. hr_avg kommt aus queryAvgHeartRate.
    d = mapping.parse_daily_health(_fix("daily_health.txt"))["2026-08-10"]
    assert "hr_avg" not in d and "hr_min" not in d and "hr_max" not in d


def test_daily_health_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_daily_health(_fix("error_anomalies.txt"))


def test_daily_health_unrecognisable_text_yields_nothing():
    # Kein Tagesmarker → leeres Ergebnis, keine Ausnahme: der Tag fehlt eben.
    assert mapping.parse_daily_health("Daily Health Data\n====\n\nNichts hier.") == {}
```

- [ ] **Step 3: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'domains.coros.mapping'`

- [ ] **Step 4: Parser schreiben**

`domains/coros/mapping.py`:

```python
"""COROS-Prosa → health_data-Spalten. Reine Funktionen, kein I/O.

Das COROS-MCP liefert keine strukturierten Daten: kein outputSchema, kein
structuredContent, nur LLM-erzeugten Text mit fester Zeilenstruktur. Dieses Modul
holt daraus die Zahlen.

Leitlinie ist Strenge. Ein fehlender Wert ist harmlos — health_scores meldet dann
ehrlich "zu wenig Daten". Ein falsch geparster Wert sieht aus wie eine Messung und
verschiebt die Baselines dauerhaft. Deshalb: exakte Muster, Plausibilitätsgrenzen,
und im Zweifel nichts.
"""
import logging
import re

log = logging.getLogger("mantis.coros")


class CorosFormatError(RuntimeError):
    """Die Antwort ist kein verwertbarer Datensatz (Fehlertext oder leer)."""


# COROS antwortet auf manche Aufrufe mit isError=false und einem Fehlertext im
# Inhalt. Diese Muster erkennen das, bevor irgendein Parser darüber läuft.
_ERROR_MARKERS = (
    "anomalies detected",
    "must be in yyyymmdd format",
    "invalid parameter",
    "no permission",
    "unauthorized",
)


def check_response(text: str) -> str:
    """Text durchreichen — oder CorosFormatError, wenn es gar keine Daten sind."""
    if not text or not text.strip():
        raise CorosFormatError("Leere Antwort vom COROS-MCP")
    low = text.lower()
    for marker in _ERROR_MARKERS:
        if marker in low:
            raise CorosFormatError(f"COROS meldet einen Fehler statt Daten: {text[:160]}")
    return text


def parse_int(raw: str) -> int | None:
    """'12,500' → 12500. 'No data', Leerstring und Unsinn → None."""
    if raw is None:
        return None
    m = re.search(r"-?\d[\d,]*", raw)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_duration_h(raw: str) -> float | None:
    """'8h 10min' → 8.17, '59 min' → 0.98, '0 min' → 0.0. Sonst None.

    Auf zwei Nachkommastellen, wie die übrigen Stundenwerte in health_data.
    """
    if raw is None:
        return None
    m = re.search(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", raw)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    hours = int(m.group(1) or 0) + int(m.group(2) or 0) / 60
    return round(hours * 100) / 100


# Plausibilitätsgrenzen (inklusive). Bewusst weit — sie sollen Parser-Ausrutscher
# abfangen (verklebte Zahlen, verrutschte Felder), keine ungewöhnlichen Tage.
LIMITS: dict[str, tuple[float, float]] = {
    "steps":            (0, 120_000),
    "active_calories":  (0, 20_000),
    "exercise_minutes": (0, 1_440),
    "stress_avg":       (0, 100),
    "sleep_duration":   (0, 24),
    "sleep_deep":       (0, 24),
    "sleep_core":       (0, 24),
    "sleep_rem":        (0, 24),
    "sleep_awake":      (0, 24),
    "resting_hr":       (20, 150),
    "hr_avg":           (20, 220),
    "hr_min":           (20, 220),
    "hr_max":           (20, 250),
    "hrv":              (1, 300),
    "vo2max":           (10, 100),
    "recovery_pct":     (0, 100),
    "sleep_score":      (0, 100),
    "training_load_short": (0, 2_000),
    "training_load_long":  (0, 2_000),
    "weight":           (20, 400),
}


def sane(col: str, value):
    """Wert zurückgeben, oder None wenn er außerhalb der Grenzen liegt."""
    if value is None:
        return None
    lo, hi = LIMITS.get(col, (float("-inf"), float("inf")))
    if lo <= value <= hi:
        return value
    log.warning(f"COROS: {col}={value} außerhalb [{lo}, {hi}] — verworfen")
    return None


def _put(target: dict, col: str, value) -> None:
    """Nur setzen, wenn ein plausibler Wert da ist. Kein None, keine 0 aus Versehen."""
    v = sane(col, value)
    if v is not None:
        target[col] = v


_DAY_RE = re.compile(r"^--- (\d{8}) ---$")


def parse_daily_health(text: str) -> dict[str, dict]:
    """queryDailyHealthData → {'2026-08-11': {'steps': …}, …}.

    Erwartetes Blockformat je Tag:

        --- 20260811 ---
        Steps: 12,500 | Calories: 1,200 kcal | Exercise: 25 min
        Stress: Avg 50
        Sleep Summary:
          Total: 6h 0min | Deep: 1h 0min | Light: 3h 30min | REM: 1h 15min | Awake: 15 min
          Sleep HR: Avg 60 bpm | Min 50 bpm | Max 85 bpm

    'Sleep HR' wird bewusst NICHT auf hr_avg/hr_min/hr_max abgebildet — das ist der
    Puls im Schlaf, nicht der des Tages. Die Tageswerte kommen aus queryAvgHeartRate.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None

    for line in text.splitlines():
        stripped = line.strip()

        m = _DAY_RE.match(stripped)
        if m:
            raw = m.group(1)
            day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            out[day] = {}
            continue
        if day is None:
            continue

        fields = out[day]

        if stripped.startswith("Steps:"):
            for label, col in (("Steps", "steps"), ("Calories", "active_calories"),
                               ("Exercise", "exercise_minutes")):
                mm = re.search(rf"{label}:\s*([^|]+)", stripped)
                if mm:
                    _put(fields, col, parse_int(mm.group(1)))
        elif stripped.startswith("Stress:"):
            _put(fields, "stress_avg", parse_int(stripped.split("Avg", 1)[1])
                 if "Avg" in stripped else None)
        elif stripped.startswith("Total:"):
            for label, col in (("Total", "sleep_duration"), ("Deep", "sleep_deep"),
                               ("Light", "sleep_core"), ("REM", "sleep_rem"),
                               ("Awake", "sleep_awake")):
                mm = re.search(rf"\b{label}:\s*([^|]+)", stripped)
                if mm:
                    _put(fields, col, parse_duration_h(mm.group(1)))

    # Tage ohne einen einzigen erkannten Wert wieder rauswerfen — sonst schreibt der
    # Importer leere Zeilen und die Scores halten den Tag für erfasst.
    return {d: f for d, f in out.items() if f}
```

- [ ] **Step 5: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: PASS, 26 Tests

- [ ] **Step 6: Gegen die echten Daten prüfen**

Die Fixtures sind synthetisch — dieser Schritt stellt sicher, dass der Parser auch die
echte Antwort verdaut. Kein Test im Repo, nur eine Sichtprüfung:

Run:
```bash
cd ~/Mantis && python3 -c "
import json, pathlib, sys; sys.path.insert(0,'.')
from domains.coros import mapping
d = json.loads(pathlib.Path('data/coros_samples/queryDailyHealthData.json').read_text())
t = json.loads(d['result']['content'][0]['text'])
days = mapping.parse_daily_health(t)
print(len(days), 'Tage')
k = sorted(days)[-1]
print(k, days[k])
"
```
Expected: die Anzahl Tage aus dem Dump und ein Tag mit gefüllten Feldern. Wenn Felder
fehlen, die im Text sichtbar sind, ist das ein Parser-Fehler — beheben und die Fixture
um den Fall erweitern.

- [ ] **Step 7: Committen**

```bash
git add domains/coros/mapping.py tests/test_coros_mapping.py tests/fixtures/coros/
git commit -m "feat(coros): Prosa-Parser mit Plausibilitätsgrenzen und Tageswerten"
```

---

### Task 2: Schlaf- und HRV-Parser

**Files:**
- Modify: `domains/coros/mapping.py`
- Create: `tests/fixtures/coros/sleep_data.txt`
- Create: `tests/fixtures/coros/sleep_hrv.txt`
- Modify: `tests/test_coros_mapping.py`

**Interfaces:**
- Consumes: `check_response`, `parse_int`, `parse_duration_h`, `_put` aus Task 1
- Produces:
  - `parse_sleep(text: str) -> dict[str, dict]` — liefert `sleep_score`
  - `parse_sleep_hrv(text: str) -> dict[str, dict]` — liefert `hrv`

`querySleepData` gibt die Schlafphasen nur als Prozentanteile ("Deep Sleep Ratio: 25%"),
`queryDailyHealthData` dagegen absolut. Absolut gewinnt — aus diesem Tool wird deshalb
**nur `sleep_score`** übernommen. Die Anteile nachzurechnen wäre eine zweite, schlechtere
Quelle für dieselben Spalten.

`querySleepHrv` liefert zuerst einen Tagesabschnitt und danach eine sehr lange Rohzeitreihe
(`timestamp=… hrv=… status=…`, über 100 KB). Der Parser liest nur den Tagesabschnitt und
muss beim Zeitreihenteil sauber aufhören, nicht daran ersticken.

- [ ] **Step 1: Fixtures anlegen**

`tests/fixtures/coros/sleep_data.txt`:

```
Sleep Data
========================
Note: each record below is dated by its wake-up day.

2026-08-10
Sleep Score: 77
Main Sleep: 7h 5min
Deep Sleep Ratio: 22%
Light Sleep Ratio: 58%
REM Ratio: 16%
Awake Ratio: 4%
Awake Time: 18 min
Awake Count (>5 min): 2
Main Sleep Window: 2026-08-09 22:35 - 2026-08-10 05:58
Naps Total: 25 min
Nap Window: 1982-08-10 12:10 - 1982-08-10 12:35

2026-08-11
Sleep Score: 38
Main Sleep: 5h 25min
Deep Sleep Ratio: 13%
Light Sleep Ratio: 62%
REM Ratio: 8%
Awake Ratio: 17%
Awake Time: 1h 5min
Awake Count (>5 min): 4
Main Sleep Window: 2026-08-10 23:05 - 2026-08-11 05:25
Naps Total: 0 min

2026-08-12
Sleep Score: No data
Main Sleep: 0 min
```

Das `Nap Window` im Jahr **1982** ist kein Tippfehler, sondern ein echter COROS-Datenfehler
aus dem Dump. Es steht hier, damit der Parser beweist, dass er kaputte Zeitstempel ignoriert
statt sie zu übernehmen.

`tests/fixtures/coros/sleep_hrv.txt`:

```
Sleep HRV — 2026-08-10 to 2026-08-12
========================
Note: dates are wake-up days (each value comes from the night that ended that morning).

HRV Assessment — Last 3 days
========================

2026-08-12:
  HRV Avg: 71 ms — Normal
  Normal Range: 40 - 60 ms
  Baseline: 48 ms
2026-08-11:
  HRV Avg: 39 ms — Below normal
  Normal Range: 40 - 60 ms
  Baseline: 48 ms
2026-08-10:
  HRV Avg: No data
  Normal Range: 40 - 60 ms
  Baseline: 48 ms

Raw Series
========================
  timestamp=1700000000, timezone=0, hrv=52 ms, status=4, confidence=90000
  timestamp=1700000600, timezone=0, hrv=44 ms, status=4, confidence=90000
```

- [ ] **Step 2: Tests ergänzen**

An `tests/test_coros_mapping.py` anhängen:

```python
# ── parse_sleep ─────────────────────────────────────────────────────────────

def test_sleep_reads_score_per_day():
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert days["2026-08-10"]["sleep_score"] == 77
    assert days["2026-08-11"]["sleep_score"] == 38


def test_sleep_no_data_score_is_omitted():
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert "2026-08-12" not in days


def test_sleep_does_not_emit_stage_columns():
    # Die Anteile sind Prozente; die absoluten Stunden kommen aus parse_daily_health.
    day = mapping.parse_sleep(_fix("sleep_data.txt"))["2026-08-10"]
    assert set(day) == {"sleep_score"}


def test_sleep_ignores_corrupt_1982_nap_window():
    # COROS liefert vereinzelt Zeitstempel von 1982 (echter Datenfehler im Dump).
    # Geschützt wird das nicht durch eine Jahresgrenze, sondern dadurch, dass nur
    # eine Zeile, die ausschliesslich aus einem Datum besteht, als Tagesmarker zählt.
    days = mapping.parse_sleep(_fix("sleep_data.txt"))
    assert not any(d.startswith("1982") for d in days)


def test_sleep_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep(_fix("error_anomalies.txt"))


# ── parse_sleep_hrv ─────────────────────────────────────────────────────────

def test_hrv_reads_daily_average():
    days = mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))
    assert days["2026-08-12"]["hrv"] == 71
    assert days["2026-08-11"]["hrv"] == 39


def test_hrv_no_data_day_is_omitted():
    assert "2026-08-10" not in mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))


def test_hrv_ignores_raw_series_section():
    # Die Rohzeitreihe enthält 'hrv=61 ms' — daraus darf kein Tag entstehen.
    days = mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))
    assert sorted(days) == ["2026-08-11", "2026-08-12"]


def test_hrv_does_not_read_baseline_as_value():
    # 'Baseline: 59 ms' steht direkt unter dem Tageswert und darf ihn nicht überschreiben.
    assert mapping.parse_sleep_hrv(_fix("sleep_hrv.txt"))["2026-08-12"]["hrv"] == 71


def test_hrv_rejects_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_sleep_hrv(_fix("error_anomalies.txt"))
```

- [ ] **Step 3: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: FAIL — `AttributeError: module 'domains.coros.mapping' has no attribute 'parse_sleep'`

- [ ] **Step 4: Parser ergänzen**

An `domains/coros/mapping.py` anhängen:

```python
_ISO_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):?$")


def parse_sleep(text: str) -> dict[str, dict]:
    """querySleepData → {'2026-08-10': {'sleep_score': 77}, …}.

    Nur der Sleep Score. Die Phasen liefert dieses Tool bloß als Prozentanteile;
    absolute Stunden kommen aus parse_daily_health, und zwei Quellen für dieselbe
    Spalte wären eine Fehlerquelle ohne Gewinn.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _ISO_DAY_RE.match(stripped)
        if m:
            day = m.group(1)
            continue
        if day and stripped.startswith("Sleep Score:"):
            fields: dict = {}
            _put(fields, "sleep_score", parse_int(stripped.split(":", 1)[1]))
            if fields:
                out[day] = fields
    return out


def parse_sleep_hrv(text: str) -> dict[str, dict]:
    """querySleepHrv → {'2026-08-12': {'hrv': 71}, …}.

    Liest nur den Tagesabschnitt. Danach folgt eine Rohzeitreihe mit zehntausenden
    'timestamp=… hrv=…'-Zeilen; die wird übersprungen, weil sie keine Tagesmarker hat.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("timestamp="):
            day = None            # Rohzeitreihe erreicht — ab hier nichts mehr übernehmen
            continue
        m = _ISO_DAY_RE.match(stripped)
        if m:
            day = m.group(1)
            continue
        if day and stripped.startswith("HRV Avg:"):
            fields: dict = {}
            _put(fields, "hrv", parse_int(stripped.split(":", 1)[1]))
            if fields:
                out[day] = fields
            day = None            # 'Baseline:' darunter darf den Wert nicht ersetzen
    return out
```

- [ ] **Step 5: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: PASS, 36 Tests

- [ ] **Step 6: Gegen die echten Daten prüfen**

Run:
```bash
cd ~/Mantis && python3 -c "
import json, pathlib, sys; sys.path.insert(0,'.')
from domains.coros import mapping
for tool, fn in [('querySleepData','parse_sleep'), ('querySleepHrv','parse_sleep_hrv')]:
    d = json.loads(pathlib.Path(f'data/coros_samples/{tool}.json').read_text())
    t = json.loads(d['result']['content'][0]['text'])
    days = getattr(mapping, fn)(t)
    print(f'{tool:16} {len(days):3} Tage, Beispiel: {sorted(days)[-1] if days else \"-\"} {days.get(sorted(days)[-1]) if days else \"\"}')
"
```
Expected: für beide Tools eine plausible Tagesanzahl und gefüllte Werte.

- [ ] **Step 7: Committen**

```bash
git add domains/coros/mapping.py tests/test_coros_mapping.py tests/fixtures/coros/
git commit -m "feat(coros): Parser für Schlaf-Score und HRV"
```

---

### Task 3: Puls, Stress, Trainingslast, Fitness

**Files:**
- Modify: `domains/coros/mapping.py`
- Create: `tests/fixtures/coros/resting_hr.txt`, `avg_hr.txt`, `training_load.txt`, `fitness_overview.txt`, `recovery.txt`
- Modify: `tests/test_coros_mapping.py`

**Interfaces:**
- Consumes: alles aus Task 1
- Produces:
  - `parse_resting_hr(text) -> dict[str, dict]` — `resting_hr`
  - `parse_avg_hr(text) -> dict[str, dict]` — `hr_avg`, `hr_min`, `hr_max`
  - `parse_training_load(text) -> dict[str, dict]` — `training_load_short`, `training_load_long`
  - `parse_fitness_overview(text) -> dict` — **tageslos**: `{'vo2max': 47}`
  - `parse_recovery(text) -> dict` — **tageslos**: `{'recovery_pct': 82}`

Die letzten beiden liefern keinen Tagesbezug, sondern den aktuellen Stand. Der Importer
schreibt sie auf den heutigen Tag; deshalb geben sie ein flaches Dict zurück, kein
Tag-Dict. Diese Asymmetrie ist Absicht und muss in den Signaturen sichtbar bleiben.

- [ ] **Step 1: Fixtures anlegen**

`tests/fixtures/coros/resting_hr.txt`:

```
Resting Heart Rate — Last 7 days
========================

2026-08-12: No data
2026-08-11: 47 bpm
2026-08-10: 46 bpm
```

`tests/fixtures/coros/avg_hr.txt`:

```
Average Heart Rate — Last 7 days
========================

2026-08-12: 72 bpm (Min: 48, Max: 120)
2026-08-11: 85 bpm (Min: 52, Max: 140)
2026-08-10: No data
```

`tests/fixtures/coros/training_load.txt`:

```
Training Load Assessment
========================

2026-08-12
Comment: Decreasing
Short-Term Load: 12
Long-Term Load: 60
Load Ratio: 0.20

2026-08-11
Comment: Decreasing
Short-Term Load: 14
Long-Term Load: 60
Load Ratio: 0.23
```

`tests/fixtures/coros/fitness_overview.txt`:

```
Fitness Assessment Overview
========================

VO2max: 47
Running Level: 70
Threshold Pace: 5:00 /km
5 km Prediction: 24:00
10 km Prediction: 50:00
Half Marathon Prediction: 1:50:00
Marathon Prediction: 3:50:00
```

`tests/fixtures/coros/recovery.txt`:

```
Recovery Status
========================

Recovery: 82%
Level: Moderate training allowed
Estimated Full Recovery: 4h
```

- [ ] **Step 2: Tests ergänzen**

An `tests/test_coros_mapping.py` anhängen:

```python
# ── parse_resting_hr / parse_avg_hr ─────────────────────────────────────────

def test_resting_hr_per_day():
    days = mapping.parse_resting_hr(_fix("resting_hr.txt"))
    assert days["2026-08-11"]["resting_hr"] == 47
    assert days["2026-08-10"]["resting_hr"] == 46


def test_resting_hr_no_data_day_omitted():
    assert "2026-08-12" not in mapping.parse_resting_hr(_fix("resting_hr.txt"))


def test_avg_hr_reads_avg_min_max():
    day = mapping.parse_avg_hr(_fix("avg_hr.txt"))["2026-08-11"]
    assert day == {"hr_avg": 85, "hr_min": 52, "hr_max": 140}


def test_avg_hr_no_data_day_omitted():
    assert "2026-08-10" not in mapping.parse_avg_hr(_fix("avg_hr.txt"))


# ── parse_training_load ─────────────────────────────────────────────────────

def test_training_load_per_day():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-12"]
    assert day == {"training_load_short": 12, "training_load_long": 60}


def test_training_load_ignores_ratio_and_comment():
    day = mapping.parse_training_load(_fix("training_load.txt"))["2026-08-11"]
    assert set(day) == {"training_load_short", "training_load_long"}


# ── tageslose Parser ────────────────────────────────────────────────────────

def test_fitness_overview_reads_vo2max_only():
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt")) == {"vo2max": 47}


def test_fitness_overview_does_not_confuse_running_level_with_vo2max():
    # 'Running Level: 70' steht direkt darunter und ist keine VO2max.
    assert mapping.parse_fitness_overview(_fix("fitness_overview.txt"))["vo2max"] == 47


def test_recovery_reads_percentage():
    assert mapping.parse_recovery(_fix("recovery.txt")) == {"recovery_pct": 82}


def test_flat_parsers_return_empty_dict_when_field_absent():
    assert mapping.parse_recovery("Recovery Status\n====\n\nLevel: unbekannt") == {}


def test_flat_parsers_reject_error_text():
    with pytest.raises(mapping.CorosFormatError):
        mapping.parse_recovery(_fix("error_anomalies.txt"))
```

- [ ] **Step 3: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: FAIL — `AttributeError: … has no attribute 'parse_resting_hr'`

- [ ] **Step 4: Parser ergänzen**

An `domains/coros/mapping.py` anhängen:

```python
def _parse_day_value_lines(text: str, pattern: re.Pattern, build) -> dict[str, dict]:
    """Gemeinsame Form für 'YYYY-MM-DD: …'-Zeilen: je Treffer ein Tag."""
    check_response(text)
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        fields = build(m)
        if fields:
            out[m.group(1)] = fields
    return out


_RHR_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}):\s*(.+)$")


def parse_resting_hr(text: str) -> dict[str, dict]:
    """queryRestingHeartRate → {'2026-08-11': {'resting_hr': 47}, …}."""
    def build(m):
        fields: dict = {}
        _put(fields, "resting_hr", parse_int(m.group(2)))
        return fields
    return _parse_day_value_lines(text, _RHR_LINE, build)


_AVG_HR_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}):\s*([^(]+?)(?:\s*\(Min:\s*([^,]+),\s*Max:\s*([^)]+)\))?$"
)


def parse_avg_hr(text: str) -> dict[str, dict]:
    """queryAvgHeartRate → {'2026-08-11': {'hr_avg': 85, 'hr_min': 52, 'hr_max': 140}, …}."""
    def build(m):
        fields: dict = {}
        _put(fields, "hr_avg", parse_int(m.group(2)))
        _put(fields, "hr_min", parse_int(m.group(3) or ""))
        _put(fields, "hr_max", parse_int(m.group(4) or ""))
        return fields
    return _parse_day_value_lines(text, _AVG_HR_LINE, build)


_PLAIN_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


def parse_training_load(text: str) -> dict[str, dict]:
    """queryTrainingLoadAssessment → kurz-/langfristige Last je Tag.

    Load Ratio und Comment werden bewusst nicht übernommen: das Ratio ist aus den
    beiden Lasten ableitbar, der Comment ist Fließtext ohne Spalte.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _PLAIN_DAY.match(stripped)
        if m:
            day = m.group(1)
            continue
        if not day:
            continue
        for label, col in (("Short-Term Load", "training_load_short"),
                           ("Long-Term Load", "training_load_long")):
            if stripped.startswith(label + ":"):
                fields = out.setdefault(day, {})
                _put(fields, col, parse_int(stripped.split(":", 1)[1]))
    return {d: f for d, f in out.items() if f}


def _parse_flat(text: str, label: str, col: str) -> dict:
    """Ein einzelnes 'Label: Wert' aus einer tageslosen Antwort."""
    check_response(text)
    fields: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label + ":"):
            _put(fields, col, parse_int(stripped.split(":", 1)[1]))
            break            # nur der erste Treffer, sonst gewinnt eine spätere Zeile
    return fields


def parse_fitness_overview(text: str) -> dict:
    """queryFitnessAssessmentOverview → {'vo2max': 47}. Ohne Tagesbezug."""
    return _parse_flat(text, "VO2max", "vo2max")


def parse_recovery(text: str) -> dict:
    """queryRecoveryStatus → {'recovery_pct': 82}. Ohne Tagesbezug."""
    return _parse_flat(text, "Recovery", "recovery_pct")
```

- [ ] **Step 5: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_mapping.py -q`
Expected: PASS, 47 Tests

- [ ] **Step 6: Gegen die echten Daten prüfen**

Run:
```bash
cd ~/Mantis && python3 -c "
import json, pathlib, sys; sys.path.insert(0,'.')
from domains.coros import mapping
pairs = [('queryRestingHeartRate','parse_resting_hr'), ('queryAvgHeartRate','parse_avg_hr'),
         ('queryTrainingLoadAssessment','parse_training_load'),
         ('queryFitnessAssessmentOverview','parse_fitness_overview'),
         ('queryRecoveryStatus','parse_recovery')]
for tool, fn in pairs:
    d = json.loads(pathlib.Path(f'data/coros_samples/{tool}.json').read_text())
    t = json.loads(d['result']['content'][0]['text'])
    print(f'{tool:32} {getattr(mapping, fn)(t)}'[:150])
"
```
Expected: gefüllte Werte für alle fünf. Leere Ergebnisse sind ein Parser-Fehler.

- [ ] **Step 7: Committen**

```bash
git add domains/coros/mapping.py tests/test_coros_mapping.py tests/fixtures/coros/
git commit -m "feat(coros): Parser für Puls, Trainingslast, VO2max und Recovery"
```

---

### Task 4: Migration, Schreibpfad und Importer

**Files:**
- Modify: `core/db.py` (MIGRATIONS-Liste)
- Modify: `domains/health.py` (neue Funktion, nichts entfernen)
- Create: `domains/coros/importer.py`
- Create: `tests/test_coros_importer.py`

**Interfaces:**
- Consumes: `CorosClient.call_tool`, `payload` aus Phase A; alle Parser aus Tasks 1-3
- Produces:
  - `health.upsert_day(day: str, fields: dict) -> bool`
  - `importer.collect(client, days: int) -> dict[str, dict]` — alle Quellen, je Tag zusammengeführt
  - `importer.sync(days: int = 14, client=None) -> int` — Anzahl geschriebener Tage

`upsert_day` ist neu **neben** `process_health_data`; entfernt wird dort in dieser Phase
nichts. Der Importer bekommt den Client injiziert, damit die Tests einen Fake einsetzen
können — dasselbe Muster wie `CorosClient(http=…)` in Phase A.

- [ ] **Step 1: Migration ergänzen**

In `core/db.py` ans Ende der `MIGRATIONS`-Liste, im dortigen Stil:

```python
    # COROS-MCP: Metriken, die über HealthKit nie ankamen (Spec 2026-08-15).
    "ALTER TABLE health_data ADD COLUMN IF NOT EXISTS training_load_short DOUBLE PRECISION;",
    "ALTER TABLE health_data ADD COLUMN IF NOT EXISTS training_load_long  DOUBLE PRECISION;",
    "ALTER TABLE health_data ADD COLUMN IF NOT EXISTS recovery_pct        DOUBLE PRECISION;",
    "ALTER TABLE health_data ADD COLUMN IF NOT EXISTS sleep_score         INT;",
    "ALTER TABLE health_data ADD COLUMN IF NOT EXISTS stress_avg          DOUBLE PRECISION;",
```

- [ ] **Step 2: Schreibpfad in domains/health.py ergänzen**

Neben `process_health_data` einfügen (nichts löschen):

```python
def upsert_day(day: str, fields: dict) -> bool:
    """Einen Tag in health_data schreiben. Formatunabhängig — wer die Felder
    erzeugt hat, ist nicht die Sache dieser Domäne.

    Idempotent über ON CONFLICT; leere Feld-Dicts werden nicht geschrieben.
    """
    if not day or not fields:
        return False
    cols = list(fields.keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO health_data (date, {', '.join(cols)}, updated_at) "
        f"VALUES (%s, {', '.join(['%s'] * len(cols))}, NOW()) "
        f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW()"
    )
    db.execute(sql, tuple([day] + [fields[c] for c in cols]))
    return True
```

- [ ] **Step 3: Test-Datei schreiben**

`tests/test_coros_importer.py`:

```python
"""Tests für den COROS-Importer (domains/coros/importer.py).

Der MCP-Client wird durch einen Fake ersetzt, der die Fixtures ausliefert; die DB
durch einen Sammel-Stub. Kein Netz, keine DB.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import importer

FIX = Path(__file__).parent / "fixtures" / "coros"

RESPONSES = {
    "queryDailyHealthData":           "daily_health.txt",
    "querySleepData":                 "sleep_data.txt",
    "querySleepHrv":                  "sleep_hrv.txt",
    "queryRestingHeartRate":          "resting_hr.txt",
    "queryAvgHeartRate":              "avg_hr.txt",
    "queryTrainingLoadAssessment":    "training_load.txt",
    "queryFitnessAssessmentOverview": "fitness_overview.txt",
    "queryRecoveryStatus":            "recovery.txt",
}


class FakeClient:
    """Liefert Fixtures im echten MCP-Result-Format. `broken` erzwingt Fehler."""

    def __init__(self, broken: set[str] | None = None):
        self.broken = broken or set()
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.broken:
            raise RuntimeError(f"{name} kaputt")
        text = (FIX / RESPONSES[name]).read_text(encoding="utf-8")
        return {"content": [{"type": "text", "text": text}]}


@pytest.fixture
def written(monkeypatch):
    """Sammelt, was upsert_day geschrieben hätte."""
    rows: dict[str, dict] = {}

    def fake_upsert(day, fields):
        if not fields:
            return False
        rows.setdefault(day, {}).update(fields)
        return True

    monkeypatch.setattr(importer.health, "upsert_day", fake_upsert)
    return rows


def test_collect_merges_sources_into_one_day():
    days = importer.collect(FakeClient(), days=14)
    d = days["2026-08-11"]
    assert d["steps"] == 12500          # queryDailyHealthData
    assert d["sleep_score"] == 38       # querySleepData
    assert d["hrv"] == 39               # querySleepHrv
    assert d["resting_hr"] == 47        # queryRestingHeartRate
    assert d["hr_avg"] == 85            # queryAvgHeartRate
    assert d["training_load_short"] == 14


def test_collect_sends_yyyymmdd_dates_not_iso():
    c = FakeClient()
    importer.collect(c, days=14)
    for name, args in c.calls:
        for key in ("startDate", "endDate"):
            if key in args:
                assert args[key].isdigit() and len(args[key]) == 8, f"{name}.{key}={args[key]}"


def test_one_broken_tool_does_not_lose_the_others():
    days = importer.collect(FakeClient(broken={"querySleepHrv"}), days=14)
    assert days["2026-08-11"]["steps"] == 12500
    assert "hrv" not in days["2026-08-11"]


def test_error_text_from_a_tool_is_treated_as_failure(monkeypatch):
    c = FakeClient()
    err = (FIX / "error_anomalies.txt").read_text(encoding="utf-8")
    real = c.call_tool

    def call(name, arguments):
        if name == "querySleepData":
            return {"content": [{"type": "text", "text": err}]}
        return real(name, arguments)

    c.call_tool = call
    days = importer.collect(c, days=14)
    assert "sleep_score" not in days["2026-08-11"]
    assert days["2026-08-11"]["steps"] == 12500


def test_flat_metrics_land_on_the_most_recent_day():
    days = importer.collect(FakeClient(), days=14)
    newest = max(days)
    assert days[newest]["vo2max"] == 47
    assert days[newest]["recovery_pct"] == 82
    older = sorted(days)[0]
    assert "vo2max" not in days[older]


def test_sync_writes_every_day_and_counts_them(written):
    n = importer.sync(days=14, client=FakeClient())
    assert n == len(written) > 0
    assert written["2026-08-11"]["steps"] == 12500


def test_sync_without_token_returns_zero_and_does_not_raise(monkeypatch, written):
    from domains.coros import oauth

    def boom():
        raise oauth.CorosNotAuthorized("nicht autorisiert")

    monkeypatch.setattr(importer, "_build_client", boom)
    assert importer.sync(days=14) == 0
    assert written == {}


def test_sync_survives_total_network_failure(monkeypatch, written):
    class DeadClient:
        def call_tool(self, name, arguments):
            raise OSError("Netz weg")

    assert importer.sync(days=14, client=DeadClient()) == 0
    assert written == {}


def test_sync_returns_partial_count_when_writing_dies(monkeypatch):
    """Eine sterbende DB darf den Hintergrund-Tick nicht sprengen."""
    calls = {"n": 0}

    def exploding_upsert(day, fields):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("DB weg")
        return True

    monkeypatch.setattr(importer.health, "upsert_day", exploding_upsert)
    assert importer.sync(days=14, client=FakeClient()) == 2
```

- [ ] **Step 4: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_importer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'domains.coros.importer'`

- [ ] **Step 5: Importer schreiben**

`domains/coros/importer.py`:

```python
"""Holt die COROS-Daten und schreibt sie nach health_data.

Ein Aufruf je Tool, alles je Tag zusammengeführt, dann ein Upsert pro Tag. Kein
gefensterter Backfill und kein Wasserstand: die gesamte Historie passt in einen
Aufruf (Spec-Nachtrag 2026-08-16), ein misslungener Lauf wird einfach wiederholt.

Ein einzelnes kaputtes Tool kostet nur seine eigenen Felder — Teildaten sind
besser als keine.
"""
import logging
from datetime import date, timedelta

from domains import health
from domains.coros import mapping, oauth
from domains.coros.client import CorosClient, payload

log = logging.getLogger("mantis.coros")

# (Tool, Parser, braucht Datumsbereich) — tageslose Parser stehen weiter unten.
_DAY_SOURCES = [
    ("queryDailyHealthData",        mapping.parse_daily_health,  False),
    ("querySleepData",              mapping.parse_sleep,         True),
    ("querySleepHrv",               mapping.parse_sleep_hrv,     True),
    ("queryRestingHeartRate",       mapping.parse_resting_hr,    False),
    ("queryAvgHeartRate",           mapping.parse_avg_hr,        False),
    ("queryTrainingLoadAssessment", mapping.parse_training_load, False),
]

_FLAT_SOURCES = [
    ("queryFitnessAssessmentOverview", mapping.parse_fitness_overview),
    ("queryRecoveryStatus",            mapping.parse_recovery),
]


def _build_client() -> CorosClient:
    oauth.access_token()          # früh scheitern, wenn nicht autorisiert
    return CorosClient()


def _args(days: int, needs_range: bool) -> dict:
    """COROS will yyyyMMdd, und manche Tools nur einen Rückblick-Zähler."""
    if not needs_range:
        return {"days": days}
    end = date.today()
    start = end - timedelta(days=days)
    return {"startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "days": days}


def collect(client, days: int) -> dict[str, dict]:
    """Alle Quellen abfragen und je Tag zusammenführen."""
    merged: dict[str, dict] = {}

    for tool, parse, needs_range in _DAY_SOURCES:
        try:
            text = payload(client.call_tool(tool, _args(days, needs_range)))
            for day, fields in parse(str(text)).items():
                merged.setdefault(day, {}).update(fields)
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    if not merged:
        return merged

    # Tageslose Metriken beschreiben den aktuellen Stand → jüngster erfasster Tag.
    newest = max(merged)
    for tool, parse in _FLAT_SOURCES:
        try:
            text = payload(client.call_tool(tool, {}))
            merged[newest].update(parse(str(text)))
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    return merged


def sync(days: int = 14, client=None) -> int:
    """Die letzten `days` Tage holen und schreiben. Gibt die Anzahl Tage zurück.

    Degradiert freundlich: fehlendes Token, fehlendes Netz und ein Schreibfehler
    ergeben je eine Logzeile und einen Rückgabewert, nie eine Ausnahme — der
    Aufrufer ist ein Hintergrund-Tick.
    """
    try:
        c = client or _build_client()
    except oauth.CorosNotAuthorized as e:
        log.info(f"COROS nicht autorisiert ({e}) — `python3 scripts/coros_auth.py` ausführen")
        return 0
    except Exception as e:
        log.warning(f"COROS-Client nicht aufgebaut: {e}")
        return 0

    try:
        days_data = collect(c, days)
    except Exception as e:
        log.warning(f"COROS-Sync fehlgeschlagen: {e}")
        return 0

    written = 0
    try:
        for day, fields in sorted(days_data.items()):
            if health.upsert_day(day, fields):
                written += 1
    except Exception as e:
        # Realistischer Fall ist eine nicht erreichbare DB, also alles-oder-nichts:
        # einmal loggen und mit dem zurückgeben, was schon durch ist, statt den
        # Hintergrund-Tick zu sprengen.
        log.warning(f"COROS-Sync: Schreiben nach {written} Tagen abgebrochen ({e})")

    if written:
        log.info(f"🩺 COROS: {written} Tage geschrieben")
    return written
```

- [ ] **Step 6: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_importer.py -q`
Expected: PASS, 9 Tests

- [ ] **Step 7: Gesamtsuite**

Run: `cd ~/Mantis && python3 -m pytest -q && python3 -m ruff check .`
Expected: keine neuen Fehlschläge, ruff sauber

- [ ] **Step 8: Committen**

```bash
git add core/db.py domains/health.py domains/coros/importer.py tests/test_coros_importer.py
git commit -m "feat(coros): Importer, Schreibpfad upsert_day und neue Spalten"
```

---

### Task 5: Die Naht umlegen und erstmalig importieren

**Files:**
- Modify: `tools/dashboard.py:98-99`
- Modify: `tests/test_coros_importer.py` (ein Test für die Naht)

**Interfaces:**
- Consumes: `importer.sync`
- Produces: nichts Neues — ab hier speist COROS die Tabelle

Ab diesem Task kommen Timos Health-Daten aus der COROS-Cloud. `core/idle_loop.py:206` und
`orchestrator.py:240` rufen `refresh_health()` weiterhin unverändert auf; nur die Quelle
dahinter wechselt.

- [ ] **Step 1: Test für die Naht schreiben**

An `tests/test_coros_importer.py` anhängen:

```python
def test_dashboard_refresh_health_calls_the_coros_importer(monkeypatch):
    """Die Naht: refresh_health muss auf COROS zeigen, nicht mehr auf den Poll."""
    from tools.dashboard import DashboardReader

    seen = {}

    def fake_sync(days=14, client=None):
        seen["days"] = days
        return 7

    monkeypatch.setattr("domains.coros.importer.sync", fake_sync)
    assert DashboardReader().refresh_health() == 7
    assert seen["days"] == 14
```

Die Klasse heißt `DashboardReader` (`tools/dashboard.py:79`) und ihr `__init__` nimmt keine
Argumente.

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_importer.py::test_dashboard_refresh_health_calls_the_coros_importer -q`
Expected: FAIL — `refresh_health` ruft noch `health_d.import_from_icloud()`

- [ ] **Step 3: Naht umlegen**

In `tools/dashboard.py` die Methode `refresh_health` ersetzen:

```python
    def refresh_health(self) -> int:
        """Health-Daten aus der COROS-Cloud nachziehen (früher: BodyOS-Poll)."""
        from domains.coros import importer
        return importer.sync()
```

Der Import steht bewusst in der Funktion: `domains/coros/importer.py` zieht `client.py`
und damit `config` nach, und `tools/dashboard.py` wird sehr früh importiert.

- [ ] **Step 4: Test laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_importer.py -q`
Expected: PASS, 9 Tests

- [ ] **Step 5: Gesamtsuite und Lint**

Run: `cd ~/Mantis && python3 -m pytest -q && python3 -m ruff check .`
Expected: keine neuen Fehlschläge, ruff sauber

- [ ] **Step 6: Committen**

```bash
git add tools/dashboard.py tests/test_coros_importer.py
git commit -m "feat(coros): refresh_health bezieht die Daten aus der COROS-Cloud"
```

- [ ] **Step 7: Erst-Import gegen die echte Cloud**

Kein Test, ein einmaliger Lauf. Braucht ein gültiges Token aus Phase A.

Run:
```bash
cd ~/Mantis && python3 -c "
import sys; sys.path.insert(0,'.')
from dotenv import load_dotenv; load_dotenv('.env')
from core import db
from domains.coros import importer
db.init_pool()
print('geschriebene Tage:', importer.sync(days=1000))
"
```
Expected: rund 94 Tage (Stand 2026-08-16). Danach prüfen:

```bash
cd ~/Mantis && python3 -c "
import sys; sys.path.insert(0,'.')
from dotenv import load_dotenv; load_dotenv('.env')
from core import db
db.init_pool()
rows = db.query('SELECT date, steps, sleep_duration, hrv, resting_hr, sleep_score, vo2max FROM health_data ORDER BY date DESC LIMIT 5')
for r in rows: print(dict(r))
print('Tage gesamt:', db.query_one('SELECT COUNT(*) AS n FROM health_data')['n'])
"
```
Expected: gefüllte Zeilen, `hrv` und `resting_hr` nicht mehr leer — genau die Lücke, die
den Umbau ausgelöst hat. Zuletzt `/api/health/scores` aufrufen und prüfen, dass Recovery
und Sleep einen Score liefern statt `insufficient_data`.

---

## Nach dieser Phase

`health_data` wird aus der COROS-Cloud gefüllt, die Scores rechnen wieder, und der alte
Push-Pfad liegt unbenutzt daneben. **Phase C** reißt ihn ab: `POST /api/health/push`,
`import_health`, `import_from_icloud`, `map_health_fields`, `process_health_data`,
`HEALTH_API_URL` und `tests/test_health_mapping.py`. Sie ist bewusst getrennt — erst
beweist der neue Pfad im Alltag, dass er trägt, dann verschwindet der alte.

Ebenfalls offen und bewusst nicht hier: der Import der Aktivitäten aus `querySportRecords`
in die `fitness`-Domäne, und die Frage, ob die neuen Metriken (Training Load, Recovery,
Sleep Score) in die Scoring-Gewichte von `health_scores.DEFAULT_CONFIG` einfließen sollen.
Beides braucht eigene Entscheidungen.
