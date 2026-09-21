"""
Jev-Entscheidungs-Engine für Mantis — dünne Schicht über `jevkit`.

Warum nicht das Ollama-Modell: Jev liefert kalibrierte Wahrscheinlichkeiten statt
`startswith("JA")`, braucht ~0.4 s und hält den globalen Ollama-GATE nicht.
Benchmark 2026-09-18 in bench/jev/ (86/92, Brier 0.048).

Vertrag (unverändert): `decide()` liefert entweder vollständige Antworten oder wirft
JevUnavailable — die Aufrufer (core/decisions.py) fallen dann auf den lokalen Pfad
zurück. Circuit-Breaker, Zeitbudget, Typ-Prüfung und Cache leben in jevkit.Client;
hier bleibt nur die Mantis-Config-Anbindung und der alte `Answer`-Typ für die Aufrufer.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
import jevkit
from jevkit import Choice, JevUnavailable, Noul, Score  # noqa: F401  (re-export für Aufrufer)

import config

log = logging.getLogger(__name__)

# Nur für Tests austauschbar (httpx.MockTransport). Produktion: None = echtes Netz.
_transport: httpx.AsyncBaseTransport | None = None
_down_until: float = 0.0


@dataclass
class Answer:
    """Kompatibilitäts-Typ für die Aufrufer in core/decisions.py."""
    kind: str                       # "noul" | "choice" | "score"
    value: bool | str | float       # Noul: p >= 0.5; Choice: Option; Score: Erwartungswert
    p: float                        # Noul: P(ja); Choice/Score: P(gewählter Wert)
    confidence: float               # Noul: |p-0.5|*2; Choice/Score: API-Wert
    probabilities: dict[str, float] = field(default_factory=dict)

    def sure(self, threshold: float = 0.5) -> bool:
        return self.confidence >= threshold

    @classmethod
    def from_jevkit(cls, a: jevkit.NoulAnswer | jevkit.ChoiceAnswer | jevkit.ScoreAnswer) -> Answer:
        if isinstance(a, jevkit.NoulAnswer):
            return cls("noul", a.value, a.p, a.confidence)
        return cls(a.kind, a.value, a.p, a.confidence, dict(a.probabilities))


def enabled() -> bool:
    """Opt-in gesetzt, Key da, kein Cooldown."""
    return bool(config.JEV_ENABLED and config.OPENROUTER_API_KEY) and time.monotonic() >= _down_until


def reset_for_tests() -> None:
    global _down_until
    _down_until = 0.0


def client() -> jevkit.Client:
    """Frischer Client aus der aktuellen Config; der Breaker-Zustand ist modulweit."""
    backend = jevkit.OpenRouterBackend(config.OPENROUTER_API_KEY, model=config.JEV_MODEL,
                                       transport=_transport, retries=0)
    c = jevkit.Client(backend, model=config.JEV_MODEL, timeout_s=config.JEV_TIMEOUT_S,
                      cooldown_s=config.JEV_COOLDOWN_S)
    c._down_until = _down_until
    return c


async def decide(state: str | dict | list, questions: dict[str, Noul | Choice | Score]) -> dict[str, Answer]:
    """Ein Call, alle Fragen parallel gegen denselben State. Wirft JevUnavailable."""
    global _down_until
    if not enabled():
        raise JevUnavailable("Jev aus, kein Key oder im Cooldown")
    c = client()
    try:
        d = await c.decide(state, questions)
    finally:
        _down_until = c._down_until
    return {qid: Answer.from_jevkit(a) for qid, a in d.answers.items()}
