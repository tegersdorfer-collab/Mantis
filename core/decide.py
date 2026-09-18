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

import asyncio
import json
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
        return Answer("choice", choice, probs[choice], float(raw["confidence"]), probs)
    # ValueError statt JevUnavailable: landet im except-Tupel von decide() und öffnet
    # so den Breaker — ein dauerhaft geändertes Schema soll nicht jeden Turn erneut
    # den vollen Roundtrip kosten, bevor lokal zurückgefallen wird.
    raise ValueError(f"unbekannter Antworttyp für {qid!r}: {raw.get('type')!r}")


async def decide(state: str | dict | list, questions: dict[str, Noul | Choice]) -> dict[str, Answer]:
    """Ein Call, alle Fragen parallel gegen denselben State. Wirft JevUnavailable.

    Eine Frage, die kein Noul/Choice ist, ist ein Bug im eigenen statischen
    Fragenkatalog — das fliegt absichtlich als AttributeError vor dem try,
    statt still auf den lokalen Pfad zurückzufallen.
    """
    global _down_until
    if not enabled():
        raise JevUnavailable("Jev aus, kein Key oder im Cooldown")
    body = {"model": config.JEV_MODEL, "state": state,
            "questions": {qid: q.payload() for qid, q in questions.items()}}
    # Serialisierung VOR dem try: ein nicht-JSON-fähiger state ist ein Caller-Bug,
    # kein Jev-Ausfall — der Breaker darf dafür nicht in Cooldown gehen.
    payload = json.dumps(body, ensure_ascii=False).encode()
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        # asyncio.timeout ist das GESAMTBUDGET (Connect+Read+Parse), httpx.timeout
        # nur pro Phase — ohne das hier kann ein langsamer Endpunkt mehrfache
        # Phasen-Timeouts addieren und den globalen Ollama-GATE trotzdem sprengen.
        async with asyncio.timeout(config.JEV_TIMEOUT_S):
            async with httpx.AsyncClient(timeout=config.JEV_TIMEOUT_S, transport=_transport) as client:
                resp = await client.post(URL, content=payload, headers=headers)
            resp.raise_for_status()
            answers = resp.json()["answers"]
            out = {qid: _parse(qid, answers[qid]) for qid in questions}
    except JevUnavailable:
        raise
    except (TimeoutError, httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as e:
        _down_until = time.monotonic() + config.JEV_COOLDOWN_S
        log.warning(f"Jev nicht erreichbar, {config.JEV_COOLDOWN_S:.0f}s lokal: {e!r}")
        raise JevUnavailable(str(e)) from e
    return out
