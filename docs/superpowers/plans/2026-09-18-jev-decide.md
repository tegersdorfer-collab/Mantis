# Jev-Entscheidungsschicht (`core/decide.py`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mantis' kleine Ja/Nein- und Kategorie-Entscheidungen laufen über TypeSafe **Jev** (via OpenRouter Decisions-Endpoint) mit kalibrierten Wahrscheinlichkeiten und Confidence-Gate; fällt Jev aus oder ist unsicher, greift der heutige lokale Ollama-Pfad unverändert.

**Architecture:** `core/decide.py` ist die Engine: typisierte Fragen (`Noul`, `Choice`) → ein HTTP-Call an `https://openrouter.ai/api/alpha/decisions` → typisierte `Answer`s mit `p`, `confidence`. Circuit-Breaker light wie in `core/backends/fallback.py` (nach Fehler Cooldown, dann wieder probieren). `core/decisions.py` enthält **alle** Fragen und Schwellen an einer Stelle (Review-freundlich) plus je einen Wrapper pro Umbaustelle, der Jev fragt und bei „nicht verfügbar / zu unsicher" den übergebenen lokalen Fallback ausführt. Vier Umbaustellen: Voice-Address-Check, Tool-Auswahl, Memory-Verifier, Memory-Konflikt-Judge.

**Tech Stack:** Python 3.14, asyncio, `httpx` 0.28 (bereits installiert), pydantic-settings (`settings.py`), Tests mit `pytest` + `asyncio.run` + `unittest.mock` (kein pytest-asyncio im Projekt), `httpx.MockTransport` für Netzwerk-Mocks.

**Spec:** Dieser Plan ist selbsttragend. Hintergrund: Benchmark `bench/jev/results/report.md` (92 Fälle, Jev 93 %, Brier 0,048; Confidence-Gate 0,5 fängt alle Ja/Nein-Fehler). API-Form: `bench/jev/run.py::jev_call`.

## Global Constraints

- **Lokal-first bleibt:** Jev ist Opt-in über `JEV_ENABLED=true` + `OPENROUTER_API_KEY`. Ohne beides verhält sich Mantis byte-identisch wie heute. Jeder Wrapper hat einen lokalen Fallback; kein Codepfad darf ohne Jev schlechter werden als vorher.
- **Timeout 3 s pro Jev-Call**, danach Fallback. Nach einem Fehler **Cooldown 120 s** ohne Jev-Versuche.
- **Confidence-Gate:** Noul gilt als sicher, wenn `|p − 0,5| · 2 ≥ 0,5` (also p ≤ 0,25 oder p ≥ 0,75). Choice gilt als sicher bei `confidence ≥ 0,5`. Unter der Schwelle → Fallback.
- **Keine neuen Abhängigkeiten.** `httpx` ist vorhanden.
- **Kommentare auf Deutsch**, Stil wie die umgebenden Dateien (knapp, Begründung statt Beschreibung).
- **Kein Ollama in Tests.** Alles gemockt. `pytest tests/` muss weiterhin grün sein.
- **Arbeitsbaum:** Worktree `.worktrees/jev-decide` auf Branch `jev/decide` (Haupt-Tree hat fremde WIP-Änderungen an `core/tools.py`, `web/index.html` u.a. — nicht anfassen).
- Die Frage-Texte in `core/decisions.py` sind **wörtlich** aus `bench/jev/run.py::jev_questions` zu übernehmen (sie sind benchmarkt).

---

## Dateistruktur

| Datei | Verantwortung |
|---|---|
| `settings.py` (modify) | `JEV_ENABLED`, `OPENROUTER_API_KEY`, `JEV_MODEL`, `JEV_TIMEOUT_S`, `JEV_COOLDOWN_S` |
| `config.py` (modify) | dieselben fünf Werte re-exportieren (Konvention) |
| `.env.example` (modify) | die zwei Opt-in-Zeilen dokumentieren |
| `core/decide.py` (create) | Fragen-Typen, `Answer`, `JevClient` (HTTP + Circuit-Breaker), `decide()` |
| `core/decisions.py` (create) | alle Fragetexte + Schwellen; Wrapper `addressed()`, `tool_categories()`, `claim_supported()`, `supersedes()` |
| `core/voice.py` (modify) | Layer 3 von `is_addressed_to_mantis` → `decisions.addressed` |
| `core/tools.py` (modify) | `select_tools_async()` = Jev-Kategorien ∪ Keyword-Ergebnis |
| `core/message_handler.py` (modify) | ruft `select_tools_async` |
| `memory/extractor.py` (modify) | Verifier → `decisions.claim_supported`; Sprecher-Fix im lokalen Prompt; Konflikt-Judge → `decisions.supersedes` |
| `tests/test_decide.py`, `tests/test_decisions.py` (create) | Engine + Wrapper |
| `tests/test_voice.py`, `tests/test_tools_select.py`, `tests/test_memory_verifier.py` (modify/create) | Umbaustellen |

---

### Task 0: Worktree anlegen

**Files:** keine Code-Änderung.

- [x] **Step 1: Worktree + Branch**

```bash
cd ~/Mantis && git worktree add .worktrees/jev-decide -b jev/decide main
cd .worktrees/jev-decide && ln -s ../../.env .env && python3 -m pytest tests -q -x --ignore=tests/test_voice.py 2>&1 | tail -3
```

Expected: bestehende Tests grün (die letzte Zeile nennt „passed"). `.env` ist ein Symlink, damit `settings.py` die echten Werte liest; **niemals committen** (steht in `.gitignore`).

Alle folgenden Tasks laufen in `~/Mantis/.worktrees/jev-decide`.

---

### Task 1: Settings

**Files:**
- Modify: `settings.py` (nach `LLM_LOCAL_ONLY`, ca. Zeile 59)
- Modify: `config.py` (nach `LLM_LOCAL_ONLY`)
- Modify: `.env.example` (nach `ANTHROPIC_API_KEY=`)
- Test: `tests/test_settings_jev.py`

**Interfaces:**
- Produces: `config.JEV_ENABLED: bool`, `config.OPENROUTER_API_KEY: str`, `config.JEV_MODEL: str`, `config.JEV_TIMEOUT_S: float`, `config.JEV_COOLDOWN_S: float`

- [x] **Step 1: Failing test**

```python
# tests/test_settings_jev.py
"""Jev ist Opt-in: ohne JEV_ENABLED + Key bleibt alles lokal."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settings import MantisSettings


def test_jev_defaults_sind_aus():
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is False
    assert s.OPENROUTER_API_KEY == ""
    assert s.JEV_MODEL == "~typesafe/jev-latest"
    assert s.JEV_TIMEOUT_S == 3.0
    assert s.JEV_COOLDOWN_S == 120.0


def test_jev_aus_env(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is True and s.OPENROUTER_API_KEY == "sk-test"
```

- [x] **Step 2: Run** `python3 -m pytest tests/test_settings_jev.py -q` → FAIL (`AttributeError: JEV_ENABLED`)

- [x] **Step 3: Implement**

In `settings.py` direkt nach der Zeile `LLM_LOCAL_ONLY: bool = False`:

```python
    # ── Jev (TypeSafe System-One-Modell via OpenRouter) ───────────────────────
    # Kleine Ja/Nein- und Kategorie-Entscheidungen (Voice-Adress-Check, Tool-
    # Auswahl, Memory-Verifier/Konflikt-Judge) laufen über Jev statt über ein
    # lokales 9B/0.5B-Modell: kalibrierte Wahrscheinlichkeiten, ~0.4 s, hält
    # den Ollama-GATE nicht. Benchmark 2026-09-18: 86/92 vs. 88 % lokal
    # (bench/jev/results/report.md). Bewusst UNABHÄNGIG von LLM_LOCAL_ONLY:
    # Timo hat dem Cloud-Call für diese Daten explizit zugestimmt (18.09.2026).
    # Ohne JEV_ENABLED oder ohne Key ist der Pfad tot und alles bleibt lokal.
    JEV_ENABLED: bool = False
    OPENROUTER_API_KEY: str = ""
    JEV_MODEL: str = "~typesafe/jev-latest"
    JEV_TIMEOUT_S: float = 3.0      # danach lokaler Fallback
    JEV_COOLDOWN_S: float = 120.0   # nach Fehler: so lange kein Jev-Versuch
```

In `config.py` nach `LLM_LOCAL_ONLY           = cfg.LLM_LOCAL_ONLY`:

```python
JEV_ENABLED              = cfg.JEV_ENABLED
OPENROUTER_API_KEY       = cfg.OPENROUTER_API_KEY
JEV_MODEL                = cfg.JEV_MODEL
JEV_TIMEOUT_S            = cfg.JEV_TIMEOUT_S
JEV_COOLDOWN_S           = cfg.JEV_COOLDOWN_S
```

In `.env.example` nach `ANTHROPIC_API_KEY=`:

```
# Jev (TypeSafe) über OpenRouter für Ja/Nein-/Kategorie-Entscheidungen. Opt-in.
JEV_ENABLED=false
OPENROUTER_API_KEY=
```

- [x] **Step 4: Run** `python3 -m pytest tests/test_settings_jev.py -q` → PASS
- [x] **Step 5: Commit** `git add settings.py config.py .env.example tests/test_settings_jev.py && git commit -m "Jev: Settings (Opt-in, Key, Modell, Timeout, Cooldown)"`

---

### Task 2: Engine `core/decide.py`

**Files:**
- Create: `core/decide.py`
- Test: `tests/test_decide.py`

**Interfaces:**
- Consumes: `config.JEV_*`, `config.OPENROUTER_API_KEY`
- Produces:
  ```python
  @dataclass(frozen=True) class Noul:   instructions: str; criteria: dict[str, str] | None = None
  @dataclass(frozen=True) class Choice: instructions: str; criteria: dict[str, str | None]
  @dataclass class Answer:
      kind: str                 # "noul" | "choice"
      value: bool | str         # Noul: p >= 0.5; Choice: gewählte Option
      p: float                  # Noul: P(ja); Choice: P(gewählte Option)
      confidence: float         # Noul: |p-0.5|*2; Choice: API-confidence
      probabilities: dict[str, float]
      def sure(self, threshold: float = 0.5) -> bool
  class JevUnavailable(Exception)
  async def decide(state, questions: dict[str, Noul | Choice]) -> dict[str, Answer]   # wirft JevUnavailable
  def enabled() -> bool
  def reset_for_tests() -> None
  ```

- [x] **Step 1: Failing tests**

```python
# tests/test_decide.py
"""core/decide.py: Jev-HTTP-Engine mit Circuit-Breaker. Kein Netz, alles per MockTransport."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from core import decide
from core.decide import Noul, Choice, Answer, JevUnavailable


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok_response(request):
    body = json.loads(request.content)
    assert body["model"] == "~typesafe/jev-latest"
    assert request.headers["authorization"] == "Bearer sk-test"
    answers = {}
    for qid, q in body["questions"].items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.93}
        else:
            answers[qid] = {"type": "choice", "choice": "flipper",
                            "probabilities": {"flipper": 0.9, "robot": 0.1}, "confidence": 0.88}
    return httpx.Response(200, json={"model": "typesafe/jev-1.13", "answers": answers,
                                     "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.0}})


def _enabled(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "sk-test")
    decide.reset_for_tests()


def test_disabled_ohne_key(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "")
    decide.reset_for_tests()
    assert decide.enabled() is False
    with pytest.raises(JevUnavailable):
        asyncio.run(decide.decide("x", {"q": Noul("Ist x?")}))


def test_noul_und_choice_werden_typisiert(monkeypatch):
    _enabled(monkeypatch)
    with patch.object(decide, "_transport", _transport(_ok_response)):
        out = asyncio.run(decide.decide({"t": "Licht an"}, {
            "a": Noul("Aktion?", criteria={"true": "ja", "false": "nein"}),
            "k": Choice("Kategorie?", {"flipper": "Lampe", "robot": None}),
        }))
    a, k = out["a"], out["k"]
    assert a.kind == "noul" and a.value is True and a.p == 0.93
    assert abs(a.confidence - 0.86) < 1e-9 and a.sure() is True
    assert k.kind == "choice" and k.value == "flipper" and k.p == 0.9 and k.confidence == 0.88
    assert k.probabilities == {"flipper": 0.9, "robot": 0.1}


def test_noul_unsicher_nahe_0_5(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {"q": {"type": "noul", "noul": 0.6}},
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})
    with patch.object(decide, "_transport", _transport(h)):
        a = asyncio.run(decide.decide("x", {"q": Noul("?")}))["q"]
    assert a.value is True and abs(a.confidence - 0.2) < 1e-9 and a.sure() is False


def test_http_fehler_wirft_und_oeffnet_breaker(monkeypatch):
    _enabled(monkeypatch)
    calls = {"n": 0}
    def h(request):
        calls["n"] += 1
        return httpx.Response(520, text="cloudflare")
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
        # Breaker offen: zweiter Call geht gar nicht mehr raus
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert calls["n"] == 1
    assert decide.enabled() is False   # während Cooldown "nicht verfügbar"


def test_breaker_schliesst_nach_cooldown(monkeypatch):
    _enabled(monkeypatch)
    monkeypatch.setattr(decide.config, "JEV_COOLDOWN_S", 0.0)
    state = {"fail": True}
    def h(request):
        if state["fail"]:
            return httpx.Response(503)
        return _ok_response(request)
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
        state["fail"] = False
        out = asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert out["q"].value is True


def test_timeout_wird_zu_unavailable(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        raise httpx.ReadTimeout("langsam")
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))


def test_fehlende_antwort_id_wirft(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
```

- [x] **Step 2: Run** `python3 -m pytest tests/test_decide.py -q` → FAIL (`ModuleNotFoundError: core.decide`)

- [x] **Step 3: Implement `core/decide.py`**

```python
"""
Jev-Entscheidungs-Engine: typisierte Fragen an TypeSafes System-One-Modell,
erreicht über den OpenRouter-Decisions-Endpoint (gleicher Body wie TypeSafe direkt).

Warum nicht das Ollama-Modell: Jev liefert kalibrierte Wahrscheinlichkeiten statt
`startswith("JA")`, braucht ~0.4 s und hält den globalen Ollama-GATE nicht.
Benchmark 2026-09-18 in bench/jev/ (86/92, Brier 0.048).

Vertrag: `decide()` liefert entweder vollständige Antworten oder wirft
JevUnavailable — die Aufrufer (core/decisions.py) fallen dann auf den lokalen
Pfad zurück. Nie halbe Ergebnisse, nie None.

Circuit-Breaker light (wie core/backends/fallback.py): nach einem Fehler gehen
Folge-Calls JEV_COOLDOWN_S lang direkt in JevUnavailable, damit ein toter
Endpunkt nicht jeden Turn erst um den Timeout verzögert.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

import config

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/alpha/decisions"

# Nur für Tests austauschbar (httpx.MockTransport). Produktion: None = echtes Netz.
_transport: httpx.AsyncBaseTransport | None = None
_down_until: float = 0.0


class JevUnavailable(Exception):
    """Jev nicht nutzbar (aus, kein Key, Cooldown, Netz/HTTP-Fehler, kaputte Antwort)."""


@dataclass(frozen=True)
class Noul:
    """Ja/Nein-Frage → P(ja)."""
    instructions: str
    criteria: dict[str, str] | None = None   # {"true": "...", "false": "..."}

    def payload(self) -> dict:
        d = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            d["criteria"] = self.criteria
        return d


@dataclass(frozen=True)
class Choice:
    """Eine Option aus einer festen Menge → Option + Verteilung + Confidence."""
    instructions: str
    criteria: dict[str, str | None]

    def payload(self) -> dict:
        return {"type": "choice", "instructions": self.instructions, "criteria": self.criteria}


@dataclass
class Answer:
    kind: str                       # "noul" | "choice"
    value: bool | str               # Noul: p >= 0.5; Choice: gewählte Option
    p: float                        # Noul: P(ja); Choice: P(gewählte Option)
    confidence: float               # Noul: |p-0.5|*2 (0 = Münzwurf); Choice: API-Wert
    probabilities: dict[str, float] = field(default_factory=dict)

    def sure(self, threshold: float = 0.5) -> bool:
        return self.confidence >= threshold


def enabled() -> bool:
    """Opt-in gesetzt, Key da, kein Cooldown."""
    return bool(config.JEV_ENABLED and config.OPENROUTER_API_KEY) and time.monotonic() >= _down_until


def reset_for_tests() -> None:
    global _down_until
    _down_until = 0.0


def _parse(qid: str, raw: dict) -> Answer:
    if raw.get("type") == "noul":
        p = float(raw["noul"])
        return Answer("noul", p >= 0.5, p, abs(p - 0.5) * 2)
    if raw.get("type") == "choice":
        probs = {k: float(v) for k, v in raw["probabilities"].items()}
        choice = raw["choice"]
        return Answer("choice", choice, probs.get(choice, 0.0), float(raw["confidence"]), probs)
    raise JevUnavailable(f"unbekannter Antworttyp für {qid!r}: {raw.get('type')!r}")


async def decide(state: str | dict | list, questions: dict[str, Noul | Choice]) -> dict[str, Answer]:
    """Ein Call, alle Fragen parallel gegen denselben State. Wirft JevUnavailable."""
    global _down_until
    if not enabled():
        raise JevUnavailable("Jev aus, kein Key oder im Cooldown")
    body = {"model": config.JEV_MODEL, "state": state,
            "questions": {qid: q.payload() for qid, q in questions.items()}}
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=config.JEV_TIMEOUT_S, transport=_transport) as client:
            resp = await client.post(URL, json=body, headers=headers)
        resp.raise_for_status()
        answers = resp.json()["answers"]
        out = {qid: _parse(qid, answers[qid]) for qid in questions}
    except JevUnavailable:
        raise
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        _down_until = time.monotonic() + config.JEV_COOLDOWN_S
        log.warning(f"Jev nicht erreichbar, {config.JEV_COOLDOWN_S:.0f}s lokal: {e!r}")
        raise JevUnavailable(str(e)) from e
    return out
```

Hinweis für den Implementierer: `KeyError` deckt die fehlende Antwort-ID ab (`answers[qid]`), `httpx.HTTPStatusError` ist ein `httpx.HTTPError`. `_transport` wird beim Aufruf gelesen (nicht beim Import), damit `patch.object(decide, "_transport", …)` greift.

- [x] **Step 4: Run** `python3 -m pytest tests/test_decide.py -q` → 7 passed
- [x] **Step 5: Commit** `git add core/decide.py tests/test_decide.py && git commit -m "Jev: Entscheidungs-Engine mit Circuit-Breaker (core/decide.py)"`

---

### Task 3: Fragenkatalog + Wrapper `core/decisions.py`

**Files:**
- Create: `core/decisions.py`
- Test: `tests/test_decisions.py`

**Interfaces:**
- Consumes: `core.decide.decide`, `Noul`, `Choice`, `Answer`, `JevUnavailable`
- Produces (alle `async`, alle mit Fallback-Parameter — der Fallback ist eine **Coroutine-Funktion ohne Argumente**, die das heutige Verhalten liefert):
  ```python
  SURE = 0.5                                  # eine Schwelle für alle Ja/Nein
  TOOL_CATEGORY_P = 0.6                       # ab dieser P(ja) gilt eine Tool-Kategorie als betroffen
  TOOL_ACTION_P = 0.7                         # ab dieser P(ja) erzwingen wir Tool-Calls
  TOOL_CATEGORY_DESCRIPTIONS: dict[str, str]  # Kategorie → deutsche Beschreibung

  async def addressed(text: str, fallback) -> bool
  async def claim_supported(user_text: str, claim: str, fallback) -> bool
  async def supersedes(old: str, new: str, fallback) -> bool
  async def tool_categories(text: str) -> tuple[set[str], bool | None] | None
      # (Kategorien mit P ≥ TOOL_CATEGORY_P, aktion True/False/None-wenn-unsicher); None wenn Jev nicht verfügbar
  ```

- [x] **Step 1: Failing tests**

```python
# tests/test_decisions.py
"""core/decisions.py: Fragen + Schwellen an einer Stelle; Fallback greift bei Ausfall UND bei Unsicherheit."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import patch

from core import decisions
from core.decide import Answer, JevUnavailable


def _noul(p: float) -> Answer:
    return Answer("noul", p >= 0.5, p, abs(p - 0.5) * 2)


async def _fallback_true():
    return True


async def _fallback_false():
    return False


def _jev_returning(answers: dict):
    async def fake(state, questions):
        fake.calls.append((state, questions))
        return {k: v for k, v in answers.items() if k in questions}
    fake.calls = []
    return fake


def _jev_down(state, questions):
    raise JevUnavailable("aus")


def test_addressed_sicher_ja_ohne_fallback():
    fake = _jev_returning({"addressed": _noul(0.97)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.addressed("mach das Licht an", _fallback_false)) is True
    state, qs = fake.calls[0]
    assert state == {"transkript": "mach das Licht an"} and set(qs) == {"addressed"}


def test_addressed_unsicher_nimmt_fallback():
    fake = _jev_returning({"addressed": _noul(0.6)})   # confidence 0.2 < SURE
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.addressed("hm ja", _fallback_true)) is True
        assert asyncio.run(decisions.addressed("hm ja", _fallback_false)) is False


def test_addressed_jev_down_nimmt_fallback():
    with patch.object(decisions.decide, "decide", _jev_down):
        assert asyncio.run(decisions.addressed("x", _fallback_true)) is True


def test_claim_supported_state_nennt_sprecher():
    fake = _jev_returning({"supported": _noul(0.05)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.claim_supported("Ich war beim Zahnarzt", "Timo hat Angst", _fallback_true)) is False
    state, _ = fake.calls[0]
    assert "Timo" in state["sprecher"] and state["text"] == "Ich war beim Zahnarzt" and state["behauptung"] == "Timo hat Angst"


def test_supersedes_state_felder():
    fake = _jev_returning({"supersedes": _noul(0.9)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.supersedes("wohnt in Leipzig", "nach Halle gezogen", _fallback_false)) is True
    state, _ = fake.calls[0]
    assert state == {"alte_aussage": "wohnt in Leipzig", "neue_aussage": "nach Halle gezogen"}


def test_tool_categories_fanout():
    answers = {f"cat:{c}": _noul(0.02) for c in decisions.TOOL_CATEGORY_DESCRIPTIONS}
    answers["cat:flipper"] = _noul(0.95)
    answers["cat:productivity"] = _noul(0.61)
    answers["aktion"] = _noul(0.9)
    fake = _jev_returning(answers)
    with patch.object(decisions.decide, "decide", fake):
        cats, aktion = asyncio.run(decisions.tool_categories("Lampe an und erinner mich später"))
    assert cats == {"flipper", "productivity"} and aktion is True
    _, qs = fake.calls[0]
    assert "aktion" in qs and len(qs) == len(decisions.TOOL_CATEGORY_DESCRIPTIONS) + 1


def test_tool_categories_aktion_unsicher_ist_none():
    answers = {f"cat:{c}": _noul(0.02) for c in decisions.TOOL_CATEGORY_DESCRIPTIONS}
    answers["aktion"] = _noul(0.55)
    with patch.object(decisions.decide, "decide", _jev_returning(answers)):
        cats, aktion = asyncio.run(decisions.tool_categories("hm"))
    assert cats == set() and aktion is None


def test_tool_categories_jev_down_ist_none():
    with patch.object(decisions.decide, "decide", _jev_down):
        assert asyncio.run(decisions.tool_categories("x")) is None


def test_beschreibungen_decken_registry_kategorien():
    """Jede Tool-Kategorie in der REGISTRY braucht eine Beschreibung, sonst kann Jev sie nie wählen."""
    import core.skills  # noqa: F401  (registriert alle Tools)
    from core.tools import REGISTRY
    fehlt = {t.category for t in REGISTRY.values()} - set(decisions.TOOL_CATEGORY_DESCRIPTIONS) - decisions.TOOL_CATEGORIES_OHNE_ROUTING
    assert not fehlt, f"ohne Beschreibung: {fehlt}"
```

- [x] **Step 2: Run** `python3 -m pytest tests/test_decisions.py -q` → FAIL (`ModuleNotFoundError`)

- [x] **Step 3: Implement `core/decisions.py`**

```python
"""
Alle Jev-Fragen und -Schwellen von Mantis an EINER Stelle.

Regel aus der TypeSafe-Doku, die sich im Benchmark bestätigt hat: Fragen und
Thresholds gehören zusammen in eine Datei, damit ein Mensch sie ohne Spelunking
reviewen kann. Die Fragetexte hier sind die aus bench/jev/run.py — die sind
gemessen (bench/jev/results/report.md). Wer sie ändert, misst nach.

Jeder Wrapper: Jev fragen → wenn nicht verfügbar ODER unter der Schwelle →
den übergebenen lokalen Fallback ausführen (das heutige Verhalten). Dadurch
kann kein Aufrufer durch Jev schlechter werden als vorher.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from core import decide
from core.decide import Choice, JevUnavailable, Noul

log = logging.getLogger(__name__)

Fallback = Callable[[], Awaitable[bool]]

SURE = 0.5             # Ja/Nein gilt als sicher ab |p-0.5|*2 >= 0.5, d.h. p <= 0.25 oder p >= 0.75
TOOL_CATEGORY_P = 0.6  # P(ja) ab der eine Tool-Kategorie als betroffen gilt
TOOL_ACTION_P = 0.7    # P(ja) ab der Tool-Calls erzwungen werden

# ── Fragen ────────────────────────────────────────────────────────────────────

Q_ADDRESSED = Noul(
    "Das Transkript ist eine Anfrage oder ein Befehl an den persönlichen Sprachassistenten im Raum "
    "(auch ohne Namensnennung), nicht Selbstgespräch oder Gespräch mit einer anderen Person.",
    criteria={"true": "Der Sprecher will, dass der Assistent reagiert",
              "false": "Beiläufiges Gerede, Selbstgespräch, Gespräch mit jemand anderem"},
)

Q_CLAIM_SUPPORTED = Noul(
    "Die Behauptung wird im Text wörtlich oder eindeutig direkt gestützt — keine Vermutung, "
    "keine Verallgemeinerung, keine Verwechslung der Person.",
    criteria={"true": "Steht so im Text", "false": "Nicht belegt, überinterpretiert oder falsche Person"},
)

Q_SUPERSEDES = Noul(
    "Die neue Aussage macht die alte veraltet oder widerspricht ihr direkt "
    "(Umzug, Wechsel, geänderte Präferenz oder Status). Zwei Dinge, die gleichzeitig wahr sein können, "
    "sind KEIN Widerspruch.",
    criteria={"true": "Alte Aussage ist jetzt überholt", "false": "Beides kann zugleich gelten"},
)

Q_ACTION = Noul(
    "Die Nachricht verlangt eine AKTION des Assistenten, für die ein Tool nötig ist "
    "(etwas anlegen, ändern, abhaken, suchen oder gespeicherte Daten abrufen).",
    criteria={"true": "Tool nötig: anlegen, ändern, abhaken, Websuche, Daten des Nutzers abrufen",
              "false": "Reines Gespräch, Meinung, Erklärung aus Allgemeinwissen, Dank"},
)

# Tool-Kategorien (core/tools.py REGISTRY → Tool.category). Ein Noul pro Kategorie,
# alle in EINEM Call (Speculative Fan-out) — Jev braucht für 20 Fragen kaum länger als für eine.
TOOL_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "fitness":      "Training, Workouts, Gewichte, Körpermaße protokollieren oder abfragen",
    "nutrition":    "Essen, Mahlzeiten, Kalorien, Ernährung protokollieren oder abfragen",
    "productivity": "Aufgaben, Termine, Erinnerungen, Kalender anlegen, ändern oder abfragen",
    "health":       "Schlaf, Puls, HRV, Erholung, Gesundheitsdaten abfragen",
    "knowledge":    "Wissensfragen, Websuche, Wetter, Notizen im Wissenssystem (Brain) speichern oder suchen",
    "habits":       "Gewohnheiten abhaken, anlegen oder Streaks abfragen",
    "goals":        "Langfristige Ziele anlegen, ändern oder Fortschritt abfragen",
    "robot":        "Den Roboter fahren, drehen, stoppen, Sensoren lesen",
    "flipper":      "Infrarot-Geräte steuern: Schreibtischlampe, Ventilator",
    "email":        "E-Mails lesen, suchen, archivieren oder einen Entwurf schreiben",
    "filesystem":   "Dateien oder Ordner auf dem Mac lesen, schreiben, öffnen; Apps öffnen",
    "geo":          "Wo liegt ein Ort, Koordinaten, Nachrichten-Briefing auf der Weltkarte",
    "gev":          "Den 3D-Globus (God's Eye View) steuern: hinfliegen, Layer, Stil",
    "journal":      "Einen Tagebuch-Eintrag schreiben",
    "memory":       "Etwas dauerhaft merken, an Gemerktes erinnern, eine Regel für den Assistenten setzen",
    "spotify":      "Musik abspielen, pausieren, weiter, was läuft gerade",
    "system":       "Den eigenen Code des Assistenten lesen oder ändern, einen neuen Skill erstellen oder löschen",
    "ui":           "Das Dashboard umbauen: Widgets zeigen, anordnen, schließen",
    "uiauto":       "Eine Mac-App per Fernsteuerung bedienen (klicken, tippen)",
    "utility":      "Etwas ausrechnen",
    "vision":       "Den Bildschirm anschauen und beschreiben",
    "skilltree":    "Den Skilltree / Fortschrittsbaum anzeigen",
}
# Kategorien, die nicht über die Nutzer-Nachricht geroutet werden (interne oder immer verfügbare Tools).
TOOL_CATEGORIES_OHNE_ROUTING: set[str] = {"general", "uiauto_internal"}


# ── Wrapper ───────────────────────────────────────────────────────────────────

async def _noul_or_fallback(name: str, state, question: Noul, fallback: Fallback) -> bool:
    try:
        ans = (await decide.decide(state, {name: question}))[name]
    except JevUnavailable:
        return await fallback()
    if not ans.sure(SURE):
        log.debug(f"Jev {name}: unsicher (p={ans.p:.2f}) → lokal")
        return await fallback()
    return bool(ans.value)


async def addressed(text: str, fallback: Fallback) -> bool:
    """Voice: Ist das Transkript an Mantis gerichtet? (Benchmark 14/14)"""
    return await _noul_or_fallback("addressed", {"transkript": text}, Q_ADDRESSED, fallback)


async def claim_supported(user_text: str, claim: str, fallback: Fallback) -> bool:
    """Memory-Verifier: Steht die extrahierte Behauptung wirklich im Text? Der Sprecher MUSS
    genannt werden — der Text sagt „ich", die Behauptung sagt „Timo" (ohne: 4/8, mit: 8/8)."""
    state = {"sprecher": "Timo (der Nutzer, spricht in der ersten Person)",
             "text": user_text[:2000], "behauptung": claim}
    return await _noul_or_fallback("supported", state, Q_CLAIM_SUPPORTED, fallback)


async def supersedes(old: str, new: str, fallback: Fallback) -> bool:
    """Memory-Konflikt: Überholt der neue Fakt den alten?"""
    return await _noul_or_fallback("supersedes", {"alte_aussage": old, "neue_aussage": new}, Q_SUPERSEDES, fallback)


async def tool_categories(text: str) -> tuple[set[str], bool | None] | None:
    """Tool-Auswahl: welche Kategorien betrifft die Nachricht, und ist es eine Aktion?
    None = Jev nicht verfügbar (Aufrufer bleibt beim Keyword-Pfad).
    aktion ist None, wenn Jev sich da nicht sicher ist."""
    questions: dict[str, Noul] = {
        f"cat:{cat}": Noul(f"Die Nachricht betrifft: {desc}.",
                           criteria={"true": "Ein Tool aus diesem Bereich wird gebraucht",
                                     "false": "Dieser Bereich ist nicht gemeint"})
        for cat, desc in TOOL_CATEGORY_DESCRIPTIONS.items()
    }
    questions["aktion"] = Q_ACTION
    try:
        answers = await decide.decide(text, questions)
    except JevUnavailable:
        return None
    cats = {cat for cat in TOOL_CATEGORY_DESCRIPTIONS if answers[f"cat:{cat}"].p >= TOOL_CATEGORY_P}
    a = answers["aktion"]
    aktion: bool | None = None
    if a.p >= TOOL_ACTION_P:
        aktion = True
    elif a.sure(SURE):
        aktion = False
    return cats, aktion
```

- [x] **Step 4: Run** `python3 -m pytest tests/test_decisions.py -q` → 9 passed. Schlägt `test_beschreibungen_decken_registry_kategorien` fehl, fehlt eine Kategorie in `TOOL_CATEGORY_DESCRIPTIONS` — ergänzen (Beschreibung aus den Tool-Docstrings ableiten), nicht in `TOOL_CATEGORIES_OHNE_ROUTING` verstecken.
- [x] **Step 5: Commit** `git add core/decisions.py tests/test_decisions.py && git commit -m "Jev: Fragenkatalog + Wrapper mit lokalem Fallback (core/decisions.py)"`

---

### Task 4: Voice-Address-Check

**Files:**
- Modify: `core/voice.py:73-96` (`is_addressed_to_mantis`)
- Test: `tests/test_voice.py` (ergänzen)

**Interfaces:**
- Consumes: `core.decisions.addressed(text, fallback)`

- [x] **Step 1: Bestehende Tests anschauen** — `sed -n 95,165p tests/test_voice.py`. Sie patchen `voice.fast.yes_no`. Das muss weiter funktionieren: mit Jev aus ist `fast.yes_no` der Fallback.

- [x] **Step 2: Failing test** (ans Ende von `tests/test_voice.py`, innerhalb des vorhandenen Import-Stils der Datei):

```python
def test_address_check_nutzt_jev_vor_fast():
    """Layer 3: Jev entscheidet; fast.yes_no ist nur noch der Fallback."""
    from unittest.mock import AsyncMock, patch
    from core import voice
    voice._conversation_active_until = 0.0
    with patch("core.voice.decisions.addressed", new=AsyncMock(return_value=True)) as jev, \
         patch("core.voice.fast.yes_no", new=AsyncMock(return_value=False)) as fast:
        assert asyncio.run(voice.is_addressed_to_mantis("mach das Licht an")) is True
    jev.assert_awaited_once()
    fast.assert_not_awaited()
    text, fallback = jev.await_args.args
    assert text == "mach das Licht an" and callable(fallback)
```

- [x] **Step 3: Run** `python3 -m pytest tests/test_voice.py -q` → neuer Test FAIL (`AttributeError: core.voice has no attribute decisions`)

- [x] **Step 4: Implement** — in `core/voice.py`: Import `from core import decisions` neben dem vorhandenen `fast`-Import ergänzen. Den Docstring-Absatz „(3) Sonst entscheidet ein kleines dediziertes Modell …" erweitern um: „Seit 18.09.2026 fragt Layer 3 zuerst Jev (core/decisions.addressed, Benchmark 14/14 statt 88 %); das lokale Modell ist Fallback bei Ausfall oder Unsicherheit." Den `return await fast.yes_no(...)`-Block ersetzen durch:

```python
    async def _lokal() -> bool:
        return await fast.yes_no(
            f"Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten "
            f"namens Mantis (nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name "
            f"'Mantis' nicht genannt wird?\n\n\"{stripped}\"",
            model=config.ADDRESS_CHECK_MODEL,
        )
    return await decisions.addressed(stripped, _lokal)
```

- [x] **Step 5: Run** `python3 -m pytest tests/test_voice.py tests/test_voice_router.py -q` → alle PASS (die alten Tests laufen mit `JEV_ENABLED=False` → `JevUnavailable` → Fallback `fast.yes_no`, wie gepatcht). Falls ein alter Test die `.env` mit `JEV_ENABLED=true` liest und dadurch echtes Netz will: in dem Test `monkeypatch.setattr(config, "JEV_ENABLED", False)` — oder besser in `tests/conftest.py` ein autouse-Fixture, das `core.decide.config.JEV_ENABLED = False` setzt (dann Task 4 Step 2 Test explizit mit `patch` auf `decisions.addressed`, was ohnehin geschieht).
- [x] **Step 6: Commit** `git add core/voice.py tests/test_voice.py tests/conftest.py && git commit -m "Voice: Adress-Check fragt Jev, lokales Modell als Fallback"`

---

### Task 5: Tool-Auswahl

**Files:**
- Modify: `core/tools.py` (nach `select_tools`, ca. Zeile 240)
- Modify: `core/message_handler.py:201-202`
- Test: `tests/test_tools_select.py` (create)

**Interfaces:**
- Consumes: `core.decisions.tool_categories(text) -> (set[str], bool|None) | None`, `TOOL_ACTION_P`
- Produces: `async def select_tools_async(text: str) -> tuple[list[str], bool]` — (erlaubte Tool-Namen, force_tools)

- [x] **Step 1: Failing tests**

```python
# tests/test_tools_select.py
"""select_tools_async: Jev-Kategorien erweitern den Keyword-Pfad, ersetzen ihn nicht."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, patch

import core.skills  # noqa: F401  registriert alle Tools
from core import tools as T


def test_ohne_jev_identisch_zum_sync_pfad():
    text = "Erinnere mich morgen um 9 an den Zahnarzt"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=None)):
        names, force = asyncio.run(T.select_tools_async(text))
    assert names == T.select_tools(text)
    assert force == (bool(names) and T.is_action(text))


def test_jev_fuegt_kategorie_hinzu_die_keywords_verpassen():
    text = "spiel was von billie eilish"   # kein Keyword für spotify in der Liste? egal — wir zwingen die Kategorie
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=({"spotify"}, True))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert "spotify" in names
    assert force is True
    # Keyword-Ergebnis bleibt enthalten
    for n in T.select_tools(text):
        assert n in names


def test_jev_aktion_unsicher_laesst_keyword_entscheiden():
    text = "Leg eine Aufgabe an: Steuer machen"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=(set(), None))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert force == (bool(names) and T.is_action(text))


def test_jev_aktion_false_ueberstimmt_keyword_nicht():
    """Jev darf Tool-Calls nicht abschalten, wenn ein Aktionswort da ist — Recall vor Precision."""
    text = "Leg eine Aufgabe an: Steuer machen"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=({"productivity"}, False))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert force is True


def test_deckel_bleibt():
    alle = set(T.decisions.TOOL_CATEGORY_DESCRIPTIONS)
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=(alle, True))):
        names, _ = asyncio.run(T.select_tools_async("alles auf einmal"))
    assert len(names) <= 14 + 3   # 14 Prefill-Deckel + die 3 Skill-Factory-Tools
```

- [x] **Step 2: Run** `python3 -m pytest tests/test_tools_select.py -q` → FAIL (`AttributeError: select_tools_async`)

- [x] **Step 3: Implement** — in `core/tools.py` oben `from core import decisions` importieren (Zirkularität prüfen: `core/decisions.py` importiert nur `core.decide` und `config`, nicht `core.tools` — okay). Nach `select_tools` einfügen:

```python
async def select_tools_async(text: str) -> tuple[list[str], bool]:
    """
    select_tools + Jev. Der Keyword-Pfad bleibt der Boden (er ist byte-identisch zu
    heute), Jev legt Kategorien obendrauf, die die Keyword-Listen verpassen — das war
    der gemma-Gotcha: „spiel [Song]" wurde von select_tools gar nicht angeboten.
    Gibt (erlaubte Tool-Namen, force_tools) zurück.
    Jev-aktion=True erzwingt Tool-Calls; aktion=False/None ändert nichts, denn ein
    Aktionswort im Text muss weiter ziehen (Recall vor Precision bei kleinen Modellen).
    """
    names = select_tools(text)
    force = bool(names) and is_action(text)
    jev = await decisions.tool_categories(text)
    if jev is None:
        return names, force
    cats, aktion = jev
    extra = [n for n, t in REGISTRY.items() if t.category in cats and n not in names]
    if extra:
        # Deckel wie in select_tools: Prefill begrenzen, Skill-Factory-Tools bleiben immer drin
        factory = [n for n in names if n in ("create_skill", "list_dynamic_skills", "delete_skill")]
        core_names = [n for n in names if n not in factory]
        names = (core_names + extra)[:14] + factory
    if aktion is True and names:
        force = True
    return names, force
```

In `core/message_handler.py` Zeilen 201–202 ersetzen:

```python
        allowed, force_tools = await skills.T.select_tools_async(text)
```

- [x] **Step 4: Run** `python3 -m pytest tests/test_tools_select.py tests/test_fast_commands.py -q` und `python3 -c "import core.message_handler"` → PASS
- [x] **Step 5: Commit** `git add core/tools.py core/message_handler.py tests/test_tools_select.py && git commit -m "Tools: select_tools_async — Jev-Kategorien ergänzen den Keyword-Pfad"`

---

### Task 6: Memory-Verifier + Konflikt-Judge

**Files:**
- Modify: `memory/extractor.py:215-233` (Verifier) und `:268-271` (Judge)
- Test: `tests/test_memory_verifier.py` (create)

**Interfaces:**
- Consumes: `core.decisions.claim_supported(user_text, claim, fallback)`, `core.decisions.supersedes(old, new, fallback)`, `memory.conflict.make_llm_judge(client, model)`

- [x] **Step 1: Failing tests**

```python
# tests/test_memory_verifier.py
"""Extractor-Verifier & Konflikt-Judge gehen über Jev; lokal ist Fallback — und der lokale
Prompt nennt jetzt den Sprecher (Text „ich" ≠ Behauptung „Timo" war ein stiller Ablehnungsgrund)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, patch

from memory import extractor as ex


def test_verify_claim_fragt_jev_mit_fallback():
    with patch("memory.extractor.decisions.claim_supported", new=AsyncMock(return_value=False)) as jev:
        ok = asyncio.run(ex.verify_claim(object(), "Ich war beim Zahnarzt", "Timo hat Angst"))
    assert ok is False
    text, claim, fallback = jev.await_args.args
    assert (text, claim) == ("Ich war beim Zahnarzt", "Timo hat Angst") and callable(fallback)


def test_lokaler_verifier_prompt_nennt_sprecher():
    prompt = ex.verify_prompt("Ich war beim Zahnarzt", "Timo war beim Zahnarzt")
    assert "Timo" in prompt and "Ich-Form" in prompt
    assert prompt.startswith("Antworte NUR mit JA oder NEIN")


def test_lokaler_verifier_fallback_parst_ja():
    class Msg:  content = "JA"
    class Resp: message = Msg()
    client = type("C", (), {"chat": AsyncMock(return_value=Resp())})()
    with patch("memory.extractor.decisions.claim_supported",
               new=AsyncMock(side_effect=lambda t, c, fb: fb())):
        assert asyncio.run(ex.verify_claim(client, "Ich war beim Zahnarzt", "Timo war beim Zahnarzt")) is True
    assert client.chat.await_args.kwargs["model"] == "qwen2.5:0.5b"


def test_judge_geht_ueber_jev():
    with patch("memory.extractor.decisions.supersedes", new=AsyncMock(return_value=True)) as jev:
        judge = ex.make_judge(object(), "qwen3.5:9b")
        assert asyncio.run(judge("wohnt in Leipzig", "nach Halle gezogen")) is True
    old, new, fallback = jev.await_args.args
    assert (old, new) == ("wohnt in Leipzig", "nach Halle gezogen") and callable(fallback)
```

- [x] **Step 2: Run** `python3 -m pytest tests/test_memory_verifier.py -q` → FAIL (`AttributeError: verify_claim`)

- [x] **Step 3: Implement** — in `memory/extractor.py`: `from core import decisions` importieren. Vor `class MemoryExtractor` drei Modul-Funktionen ergänzen:

```python
def verify_prompt(user_text: str, claim: str) -> str:
    """Lokaler Verifier-Prompt. Der Sprecher MUSS genannt sein: Der Text ist in Ich-Form,
    die Behauptung sagt „Timo" — ohne diesen Satz lehnte der 0.5B-Verifier gültige
    Ich-Fakten ab (bench/jev: 4/8 → 8/8 allein durch die Sprecher-Angabe)."""
    return (
        f"Antworte NUR mit JA oder NEIN, nichts anderes.\n\n"
        f"Der Text stammt von Timo und ist in der Ich-Form geschrieben (\"ich\" = Timo).\n"
        f"Text: \"{user_text[:500]}\"\n"
        f"Behauptung: \"{claim}\"\n\n"
        f"Wird die Behauptung im Text wörtlich oder eindeutig direkt erwähnt?"
    )


async def verify_claim(client, user_text: str, claim: str) -> bool:
    """Steht die Behauptung wirklich im Text? Jev zuerst, lokal (qwen2.5:0.5b) als Fallback."""
    async def _lokal() -> bool:
        vresp = await client.chat(
            model="qwen2.5:0.5b",
            messages=[{"role": "user", "content": verify_prompt(user_text, claim)}],
            options={"temperature": 0.0, "num_predict": 5, "keep_alive": "5m"},
            think=False,
        )
        return (vresp.message.content or "").strip().upper().startswith("JA")
    return await decisions.claim_supported(user_text, claim, _lokal)


def make_judge(client, model: str):
    """Konflikt-Judge: Jev zuerst, sonst der bisherige LLM-Judge aus memory/conflict.py."""
    from memory import conflict
    lokal = conflict.make_llm_judge(client, model)

    async def _judge(old: str, new: str) -> bool:
        async def _fb() -> bool:
            return await lokal(old, new)
        return await decisions.supersedes(old, new, _fb)
    return _judge
```

Dann im Extraktor den Block „4b. Verifier" (das `try:` mit `verify_prompt = (...)`, dem `self._client.chat(...)`-Call und `verdict`) ersetzen durch:

```python
            # ── 4b. Verifier: steht das wirklich im Text? (Jev, lokal 0.5B als Fallback) ──
            try:
                if not await verify_claim(self._client, user_text, text):
                    log.debug(f"Extraktor: Verifier abgelehnt: '{text[:60]}'")
                    continue
            except Exception as ve:
                log.debug(f"Verifier fehlgeschlagen, überspringe Check: {ve}")
```

Und bei „6b. Konfliktauflösung" die Zeile `judge = conflict.make_llm_judge(self._client, config.AGENT_MODEL_FAST)` ersetzen durch `judge = make_judge(self._client, config.AGENT_MODEL_FAST)` (der `from memory import conflict`-Import in dem Block kann bleiben, wird für `conflict.resolve` gebraucht).

- [x] **Step 4: Run** `python3 -m pytest tests/test_memory_verifier.py tests/ -q -k "memory or extractor or conflict or dedup"` → PASS
- [x] **Step 5: Commit** `git add memory/extractor.py tests/test_memory_verifier.py && git commit -m "Memory: Verifier + Konflikt-Judge über Jev; lokaler Verifier-Prompt nennt den Sprecher"`

---

### Task 7: Gesamttest, Live-Probe, Doku

**Files:**
- Modify: `README.md` (Abschnitt LLM-Stack / Konfiguration — kurzer Absatz „Jev")
- Modify: `docs/superpowers/plans/2026-09-18-jev-decide.md` (Checkboxen)

- [x] **Step 1: Volle Suite** `python3 -m pytest tests -q 2>&1 | tail -3` → alles grün; Anzahl gegenüber Task 0 gestiegen.

- [x] **Step 2: Live-Probe gegen echtes Jev** (nur wenn `.env` `JEV_ENABLED=true` hat; setzt der Ausführende **nicht** selbst — Timo entscheidet das per `.env`). Wenn gesetzt:

```bash
cd ~/Mantis/.worktrees/jev-decide && python3 - <<'EOF'
import asyncio, sys; sys.path.insert(0, ".")
import core.skills
from core import tools, decisions
async def main():
    for t in ["spiel was von billie eilish", "mach das Licht an", "wie hab ich geschlafen", "danke dir"]:
        names, force = await tools.select_tools_async(t)
        print(f"{t!r:40} force={force!s:5} {names[:6]}")
    async def nein(): return False
    print("addressed:", await decisions.addressed("boah bin ich müde", nein), await decisions.addressed("erinner mich an die wäsche", nein))
asyncio.run(main())
EOF
```

Expected: `spotify` in der ersten Zeile, `lampe` in der zweiten, `get_health` in der dritten, vierte Zeile `force=False`; `addressed: False True`. Kein Ollama nötig, nichts wird geladen.

- [x] **Step 3: README** — im Konfigurations-/LLM-Abschnitt drei Sätze: was Jev ist, dass es Opt-in über `JEV_ENABLED` + `OPENROUTER_API_KEY` ist, dass alle Fragen/Schwellen in `core/decisions.py` stehen und der Benchmark in `bench/jev/` liegt.

- [x] **Step 4: Commit** `git add README.md docs/superpowers/plans/2026-09-18-jev-decide.md && git commit -m "Jev: README + Plan-Abschluss"`

- [x] **Step 5: Übergabe** — Branch `jev/decide` ist fertig; **nicht** selbst mergen (Haupt-Tree hat fremde WIP in `core/tools.py`; der Merge ist ein 3-Zeilen-Konflikt in `_CATEGORY_KEYWORDS` und gehört Timo bzw. dem Abschluss-Skill `superpowers:finishing-a-development-branch`).

---

## Self-Review

- **Abdeckung:** Settings (T1), Engine + Breaker + Timeout (T2), Fragen/Schwellen an einer Stelle + vier Wrapper (T3), Voice (T4), Tools + message_handler (T5), Verifier + Sprecher-Fix + Judge (T6), Suite/Live/Doku (T7). Der Punkt „Lokal-first / byte-identisch ohne Jev" ist in T3 (Fallback bei `JevUnavailable`), T5 (`test_ohne_jev_identisch_zum_sync_pfad`) und T4/T6 (Fallback = alter Code) abgesichert.
- **Typen:** `decisions.tool_categories` liefert `tuple[set[str], bool|None] | None` — so in T3 definiert, in T5 konsumiert. `Answer.sure(threshold)` in T2 definiert, in T3 genutzt. `verify_claim(client, user_text, claim)` und `make_judge(client, model)` in T6 definiert und dort getestet. `select_tools_async` → `tuple[list[str], bool]` in T5 definiert und in `message_handler` entpackt.
- **Platzhalter:** keine; alle Codeblöcke vollständig.
