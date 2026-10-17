"""Cloud-Provider-Kette für Jev-Entscheidungen.

Cloudflare Clef wird zuerst versucht, danach die konfigurierten Jev-Provider. Eine
leere ``JEV_PROVIDER_CHAIN`` verwendet aus Rückwärtskompatibilität ausschließlich
``JEV_PROVIDER``. ``decide()`` liefert vollständige Antworten oder wirft
``JevUnavailable``; die Aufrufer in ``core/decisions.py`` fragen dann die Fast-Cloud-Kette.
Jeder Provider hat einen eigenen Circuit-Breaker. Pro Versuch gilt
``JEV_TIMEOUT_S``, über alle Versuche zusammen höchstens das Doppelte.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import jevkit
from jevkit import Choice, JevUnavailable, Noul, Score  # noqa: F401  (re-export für Aufrufer)

import config
from core.decide_cloudflare import CloudflareBackend, CloudflareQuotaError

log = logging.getLogger(__name__)

# Nur für Tests austauschbar (httpx.MockTransport). Produktion: None = echtes Netz.
_transport: httpx.AsyncBaseTransport | None = None
_down_until: dict[str, float] = {}
_active_provider: str | None = None
_INVALID_QUESTION_ID_CHARACTERS = re.compile(r"[^A-Za-z0-9_.-]+")


def _monotonic() -> float:
    """Wrappbarer Zeitgeber für Gesamtbudget und Circuit-Breaker."""
    return time.monotonic()


def _utcnow() -> datetime:
    """Wrappbarer UTC-Zeitgeber für Cloudflares Tageskontingent."""
    return datetime.now(timezone.utc)


def _sanitize_question_ids(
    questions: dict[str, Noul | Choice | Score],
) -> tuple[dict[str, Noul | Choice | Score], dict[str, str]]:
    """Jev-IDs erfüllen das API-Pattern; Antworten werden intern zurückbenannt."""
    safe_questions = {}
    internal_ids = {}
    for internal_id, question in questions.items():
        base = _INVALID_QUESTION_ID_CHARACTERS.sub(".", str(internal_id))[:100] or "question"
        safe_id = base
        suffix = 2
        while safe_id in internal_ids:
            marker = f"_{suffix}"
            safe_id = f"{base[:100 - len(marker)]}{marker}"
            suffix += 1
        safe_questions[safe_id] = question
        internal_ids[safe_id] = internal_id
    return safe_questions, internal_ids


def _is_request_validation_error(error: BaseException | None) -> bool:
    if error is None:
        return False
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code in {400, 422}:
        return True
    return bool(re.match(r"^HTTP (?:400|422):", str(error)))

# Beide Anbieter liefern IDs für dieselbe jevkit-Modellversion. Je Provider
# übersetzt das jeweilige Backend den Pin in die passende ID.
JEV_EXPECTED_MODELS = {
    "cloudflare": str(config.CLEF_MODEL).strip() or "clef-flash",
    "typesafe": jevkit.PINNED,
    "openrouter": jevkit.PINNED,
}
# Kompatibilität für bench/jev/live_path.py und externe Auswertung.
JEV_EXPECTED_MODEL = JEV_EXPECTED_MODELS["typesafe"]


@dataclass
class Answer:
    """Kompatibilitäts-Typ für die Aufrufer in core/decisions.py."""
    kind: str                       # "noul" | "choice" | "score"
    value: bool | str | float       # Noul: p >= 0.5; Choice: Option; Score: Erwartungswert
    p: float                        # Noul: P(ja); Choice/Score: P(gewählter Wert)
    confidence: float               # Noul: |p-0.5|*2; Choice/Score: API-Wert
    probabilities: dict[str, float] = field(default_factory=dict)
    model: str = ""                 # Decision.model (welches Modell geantwortet hat)
    raw: object = None              # die rohe jevkit-Answer, fürs Log (core/decisions.py)
    metadata: dict[str, object] = field(default_factory=dict)  # lokale Hinweise, z.B. fehlende Logit-Labels

    def sure(self, threshold: float = 0.5) -> bool:
        return self.confidence >= threshold

    @classmethod
    def from_jevkit(cls, a: jevkit.NoulAnswer | jevkit.ChoiceAnswer | jevkit.ScoreAnswer,
                    model: str = "") -> Answer:
        if isinstance(a, jevkit.NoulAnswer):
            return cls("noul", a.value, a.p, a.confidence, model=model, raw=a)
        if isinstance(a, (jevkit.ChoiceAnswer, jevkit.ScoreAnswer)):
            probabilities = dict(a.probabilities)
            return cls(a.kind, a.value, a.p, a.confidence, probabilities, model=model, raw=a)
        raise TypeError(f"unsupported Jev answer type: {type(a).__name__}")


def enabled() -> bool:
    """True, wenn mindestens ein aktivierter Provider Credentials hat und verfügbar ist."""
    if not config.JEV_ENABLED:
        return False
    now = _monotonic()
    return any(
        provider in JEV_EXPECTED_MODELS
        and _provider_has_credentials(provider)
        and now >= _down_until.get(provider, 0.0)
        for provider in provider_chain()
    )


def provider_chain() -> list[str]:
    """Gibt die konfigurierte Kette oder den kompatiblen Einzel-Provider zurück."""
    configured = str(getattr(config, "JEV_PROVIDER_CHAIN", "") or "")
    providers = list(dict.fromkeys(part.strip().lower() for part in configured.split(",") if part.strip()))
    if providers:
        return providers
    legacy = str(config.JEV_PROVIDER).strip().lower()
    return [legacy] if legacy else []


def expected_model(provider: str) -> str | jevkit.JevModel:
    """Modellversion, die jevkit für den gewählten Provider erwarten soll."""
    if provider == "cloudflare":
        return str(config.CLEF_MODEL).strip() or "clef-flash"
    return JEV_EXPECTED_MODELS[provider]


def _provider_has_credentials(provider: str) -> bool:
    if provider == "cloudflare":
        return bool(str(config.CLOUDFLARE_ACCOUNT_ID).strip() and str(config.CLOUDFLARE_AUTH_TOKEN).strip())
    if provider == "openrouter":
        return bool(str(config.OPENROUTER_API_KEY).strip())
    if provider == "typesafe":
        return bool(str(config.TYPESAFE_API_KEY).strip())
    return False


def reset_for_tests() -> None:
    global _active_provider
    _down_until.clear()
    _active_provider = None


def client(provider: str | None = None, *, timeout_s: float | None = None) -> jevkit.Client:
    """Frischer Client für einen Provider; dessen Breaker-Zustand ist modulweit."""
    provider = (provider or (provider_chain() or [""])[0]).strip().lower()
    configured_model = str(config.JEV_MODEL or "").strip()
    if provider == "typesafe":
        model = configured_model.removeprefix("~typesafe/") if configured_model else jevkit.PINNED.typesafe_id
        backend_options = {"transport": _transport, "retries": 0}
        if configured_model:
            backend_options["model"] = model
        backend = jevkit.TypeSafeBackend(config.TYPESAFE_API_KEY, **backend_options)
    elif provider == "openrouter":
        model = configured_model or jevkit.PINNED.openrouter_id
        backend_options = {"transport": _transport, "retries": 0}
        if configured_model:
            if not model.startswith(("~typesafe/", "typesafe/")):
                model = f"~typesafe/{model}"
            backend_options["model"] = model
        backend = jevkit.OpenRouterBackend(config.OPENROUTER_API_KEY, **backend_options)
    elif provider == "cloudflare":
        model = str(config.CLEF_MODEL).strip() or "clef-flash"
        backend = CloudflareBackend(
            config.CLOUDFLARE_ACCOUNT_ID,
            config.CLOUDFLARE_AUTH_TOKEN,
            model=model,
            transport=_transport,
        )
    else:
        raise JevUnavailable(f"unbekannter Jev-Provider: {provider}")
    c = jevkit.Client(backend, model=model, timeout_s=timeout_s or config.JEV_TIMEOUT_S,
                      cooldown_s=config.JEV_COOLDOWN_S,
                      expected_model=expected_model(provider), clock=_monotonic)
    c._down_until = _down_until.get(provider, 0.0)
    return c


async def decide(state: str | dict | list, questions: dict[str, Noul | Choice | Score]) -> dict[str, Answer]:
    """Fragt Provider der Reihe nach; wirft JevUnavailable, wenn keiner antwortet."""
    if not enabled():
        raise JevUnavailable("Jev aus, kein Provider mit Credentials oder alle im Cooldown")

    provider_questions, internal_ids = _sanitize_question_ids(questions)
    providers = provider_chain()
    configured_timeout = max(0.0, float(config.JEV_TIMEOUT_S))
    started = _monotonic()
    deadline = started + 2 * configured_timeout
    attempted = False
    last_provider = ""
    switch_reason = "Fallback in der konfigurierten Provider-Kette"

    global _active_provider
    if _active_provider is None and providers:
        _active_provider = providers[0]

    for provider in providers:
        if provider not in JEV_EXPECTED_MODELS or not _provider_has_credentials(provider):
            switch_reason = f"{provider} ohne Credentials oder unbekannt"
            continue
        now = _monotonic()
        if now < _down_until.get(provider, 0.0):
            switch_reason = f"{provider} im Cooldown"
            continue
        remaining = deadline - now
        if remaining <= 0 or configured_timeout <= 0:
            break
        attempt_timeout = min(configured_timeout, remaining)

        if _active_provider != provider:
            log.info("Jev-Providerwechsel: %s → %s (%s)", _active_provider, provider, switch_reason)
            _active_provider = provider

        c = client(provider, timeout_s=attempt_timeout)
        attempted = True
        last_provider = provider
        try:
            total_remaining = max(0.0, deadline - _monotonic())
            async with asyncio.timeout(total_remaining):
                d = await c.decide(state, provider_questions)
        except TimeoutError:
            c._down_until = max(c._down_until, _monotonic() + config.JEV_COOLDOWN_S)
            switch_reason = f"{provider} Zeitbudget überschritten"
            continue
        except JevUnavailable as exc:
            cause = exc.__cause__
            if provider == "cloudflare" and isinstance(cause, CloudflareQuotaError):
                reset_at = _next_utc_midnight()
                seconds_until_reset = max(0.0, (reset_at - _utcnow()).total_seconds())
                c._down_until = _monotonic() + seconds_until_reset
                switch_reason = f"Cloudflare-Tageskontingent bis {reset_at:%Y-%m-%d %H:%M UTC}"
            elif _is_request_validation_error(cause):
                # jevkit setzt für jeden HTTP-Fehler intern einen Cooldown; ein
                # ungültiger Einzelrequest ist aber kein Provider-Ausfall.
                c._down_until = _down_until.get(provider, 0.0)
                switch_reason = f"{provider} Request abgelehnt"
            else:
                switch_reason = f"{provider} nicht verfügbar"
            continue
        finally:
            # Nur vorwärts spiegeln: parallele Calls dürfen einen geöffneten
            # Breaker nicht mit einem älteren Stand überschreiben (Lost Update).
            _down_until[provider] = max(_down_until.get(provider, 0.0), c._down_until)

        return {
            internal_ids.get(qid, qid): Answer.from_jevkit(a, model=d.model)
            for qid, a in d.answers.items()
        }

    if attempted:
        log.info("Jev-Providerwechsel: %s → Fast-Cloud-Fallback (%s)", last_provider, switch_reason)
        _active_provider = "cloud"
    raise JevUnavailable("Alle Jev-Provider sind fehlgeschlagen oder nicht verfügbar")


def _next_utc_midnight() -> datetime:
    now = _utcnow().astimezone(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
