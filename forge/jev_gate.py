"""Optionale Jev-Vorprüfung für Forge-Tasks.

Jev liefert hier nur Klassifikation und Risikokontext. Es erteilt keine
Außenfreigabe: ein erkannter externer/destruktiver Wunsch wird vor dem ersten
Agentenlauf geparkt, und der deterministische Gate-/Freigabeweg bleibt davon
unberührt.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import jevkit
from jevkit import Choice, Noul, Score

import config
from core import decide

log = logging.getLogger(__name__)

PREFLIGHT_DATEI = ".forge/jev-preflight.json"
MODI = ("plan", "bugfix", "tdd", "security_review")
RISIKEN = ("routine", "sensitive", "high", "critical")
_STATUS = frozenset({"ready", "blocked", "disabled", "unavailable"})
_MODE_CONFIDENCE = 0.5
_RISK_CONFIDENCE = 0.35


@dataclass(frozen=True)
class PreflightResult:
    status: str
    mode: str = "plan"
    risk: str = "unknown"
    external: bool = False
    reason: str = ""

    def as_context(self) -> dict[str, str]:
        """Nur validierte, nicht-sensitive Werte für Forge-Prompts."""
        return {"jev_mode": self.mode, "jev_risk": self.risk}


Q_MODE = Choice(
    "Welcher Arbeitsmodus beschreibt die technische Aufgabe am besten?",
    {
        "plan": "Anforderung ist unklar oder braucht zuerst eine belastbare Spezifikation",
        "bugfix": "Ein bestehender Fehler soll reproduziert und behoben werden",
        "tdd": "Eine neue Funktion oder Änderung soll testgetrieben umgesetzt werden",
        "security_review": "Sicherheitsverhalten, Berechtigungen oder Vertrauensgrenzen sind zentral",
    },
)
Q_RISK = Score(
    "Wie hoch ist das technische Änderungsrisiko der Aufgabe, unabhängig von einer Merge-Freigabe?",
    ("routine", "sensitive", "high", "critical"),
)
Q_EXTERNAL = Noul(
    "Verlangt die Aufgabenbeschreibung eine externe oder destruktive Nebenwirkung "
    "wie Push, Deployment, Löschen, Veröffentlichung oder eine reale Systemänderung?",
    criteria={
        "true": "Eine solche Nebenwirkung wird verlangt oder eindeutig beschrieben",
        "false": "Die Arbeit bleibt auf Analyse, Tests und Dateien im isolierten Worktree beschränkt",
    },
)


def _state(task: dict) -> dict[str, str]:
    """Begrenzt und markiert Queue-Text als Fremddaten für Jevs Guard."""
    payload = {
        "title": str(task.get("title") or "")[:2000],
        "description": str(task.get("description") or "")[:4000],
    }
    return jevkit.untrusted(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _mode(answer: decide.Answer) -> str:
    if answer.kind == "choice" and answer.confidence >= _MODE_CONFIDENCE and answer.value in MODI:
        return str(answer.value)
    return "plan"


def _risk(answer: decide.Answer) -> str:
    if answer.kind != "score" or answer.confidence < _RISK_CONFIDENCE:
        return "unknown"
    try:
        level = int(round(float(answer.value)))
    except (TypeError, ValueError):
        return "unknown"
    return RISIKEN[level] if 0 <= level < len(RISIKEN) else "unknown"


def _log_answers(answers: dict[str, decide.Answer], state: dict[str, str]) -> None:
    """Loggt nur Jev-Antworten und einen State-Hash, niemals den Tasktext."""
    path = getattr(config, "JEV_LOG_PATH", "")
    if not path:
        return
    try:
        raw = {qid: answer.raw for qid, answer in answers.items() if answer.raw is not None}
        if not raw:
            return
        decision = jevkit.Decision(
            raw,
            next((answer.model for answer in answers.values() if answer.model), "unknown"),
            {}, False, 0.0,
        )
        bands = {
            qid: jevkit.band(answer.raw, jevkit.Bands(act=0.5, escalate=0.2))
            for qid, answer in answers.items() if answer.raw is not None
        }
        jevkit.DecisionLog(Path(path)).write(decision, bands, state)
    except Exception as exc:  # Logging darf die Sicherheitsentscheidung nicht verändern.
        log.debug("Forge-Jev-Log fehlgeschlagen: %r", exc)


async def entscheide(task: dict) -> PreflightResult:
    """Klassifiziert einen Task oder liefert einen sicheren No-op-Fallback."""
    if not getattr(config, "JEV_FORGE_GATE_ENABLED", False):
        return PreflightResult(status="disabled")

    questions = jevkit.with_guard({
        "mode": Q_MODE,
        "risk": Q_RISK,
        "external": Q_EXTERNAL,
    })
    try:
        answers = await decide.decide(_state(task), questions)
    except decide.JevUnavailable:
        return PreflightResult(status="unavailable")
    except Exception as exc:  # Jev darf keinen Forge-Task aktiv hängen lassen.
        log.warning("Forge-Jev-Gate nicht verfügbar: %s", type(exc).__name__)
        return PreflightResult(status="unavailable")

    state = _state(task)
    _log_answers(answers, state)

    try:
        guard = answers.get(jevkit.GUARD_ID)
        if guard is None or guard.p >= 0.5:
            return PreflightResult(
                status="blocked", mode="security_review", risk="critical",
                reason="Jev-Guard hat den Tasktext als potenziell manipuliert erkannt",
            )
        mode = _mode(answers["mode"])
        risk = _risk(answers["risk"])
        external_answer = answers["external"]
    except (KeyError, AttributeError, TypeError):
        return PreflightResult(
            status="blocked", mode="security_review", risk="critical",
            reason="Jev-Vorprüfung lieferte kein vollständiges, verwertbares Ergebnis",
        )
    external = external_answer.kind == "noul" and external_answer.p >= 0.5

    reasons = []
    if external:
        reasons.append("externe oder destruktive Absicht erkannt")
    if risk in {"high", "critical"}:
        reasons.append(f"Risikostufe {risk}")
    if reasons:
        return PreflightResult(
            status="blocked", mode=mode, risk=risk, external=external,
            reason="; ".join(reasons),
        )
    return PreflightResult(status="ready", mode=mode, risk=risk, external=external)


def _aus_dict(payload: object) -> PreflightResult | None:
    if not isinstance(payload, dict):
        return None
    status, mode = payload.get("status"), payload.get("mode")
    risk, external = payload.get("risk"), payload.get("external")
    reason = payload.get("reason", "")
    if (status not in _STATUS or mode not in MODI or
            risk not in (*RISIKEN, "unknown") or not isinstance(external, bool) or
            not isinstance(reason, str)):
        return None
    return PreflightResult(status, mode, risk, external, reason)


def lade(worktree: Path) -> PreflightResult | None:
    datei = Path(worktree) / PREFLIGHT_DATEI
    try:
        return _aus_dict(json.loads(datei.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def speichere(worktree: Path, result: PreflightResult) -> None:
    """Schreibt die interne Entscheidung atomar in den Task-Worktree."""
    ziel = Path(worktree) / PREFLIGHT_DATEI
    ziel.parent.mkdir(parents=True, exist_ok=True)
    temporaer = ziel.with_name(ziel.name + ".tmp")
    temporaer.write_text(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True) + "\n")
    temporaer.replace(ziel)


def preflight(task: dict, worktree: Path) -> PreflightResult:
    """Liest eine vorhandene Vorprüfung oder führt sie genau einmal aus."""
    # Ein späteres Abschalten muss auch einen alten Cache unwirksam machen;
    # sonst würde ein früheres Jev-Ergebnis die Opt-out-Einstellung überleben.
    if not (getattr(config, "JEV_FORGE_GATE_ENABLED", False) and
            getattr(config, "JEV_ENABLED", False)):
        return PreflightResult(status="disabled")
    vorhanden = lade(worktree)
    if vorhanden is not None:
        return vorhanden
    result = asyncio.run(entscheide(task))
    if result.status in {"ready", "blocked"}:
        speichere(worktree, result)
    return result
