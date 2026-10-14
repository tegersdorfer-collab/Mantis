"""Aufbau der gemeinsamen Cloud-LLM-Kette für alle Laufzeitzwecke.

Persönliche und gespeicherte Daten dürfen an die Cloud-Anbieter gehen. Die
einzige lokale LLM-Ausnahme sind Embeddings; sie werden nicht über diese Fabrik
angefragt.
"""
import logging
from pathlib import Path

import config
from core.auth.chatgpt import is_auth_configured
from core.backends.chatgpt_plan import ChatGPTPlanBackend
from core.backends.claude import ClaudeBackend
from core.backends.fallback import FallbackBackend
from core.backends.llm_adapter import BackendLLMAdapter
from core.backends.openrouter import OpenRouterBackend

log = logging.getLogger(__name__)


class CloudChainUnavailableError(RuntimeError):
    """Kein Cloud-Anbieter ist für die angeforderte LLM-Kette konfiguriert."""


def warn_if_local_only(cfg=config) -> None:
    """Warnt beim Start, wenn das alte Flag lokale LLMs erwartet."""
    if getattr(cfg, "LLM_LOCAL_ONLY", False):
        log.warning(
            "LLM_LOCAL_ONLY=True ist gesetzt, lokale LLM-Modelle sind entfernt; "
            "Mantis nutzt weiterhin die Cloud-Kette."
        )


def _openrouter_models(cfg) -> list[str]:
    if not str(getattr(cfg, "OPENROUTER_API_KEY", "") or "").strip():
        return []
    primary = str(getattr(cfg, "CHAT_CLOUD_MODEL", "") or "").strip()
    if not primary:
        return []
    models = [primary]
    fallback = str(getattr(cfg, "CHAT_CLOUD_FALLBACK_MODEL", "") or "").strip()
    if fallback and fallback != primary:
        models.append(fallback)
    return models


def chatgpt_auth_is_configured(cfg) -> bool:
    auth_file = Path(str(getattr(cfg, "CHATGPT_AUTH_FILE", "~/.config/mantis/chatgpt_auth.json"))).expanduser()
    return is_auth_configured(auth_file)


def _cloud_backends(cfg, purpose: str, reasoning: str | None) -> list:
    stages = []
    models = _openrouter_models(cfg)
    if purpose != "chat-agent":
        models = models[:1]
    if models:
        effort = reasoning if reasoning is not None else getattr(
            cfg, "CHAT_CLOUD_REASONING_EFFORT", "low"
        )
        stages.extend(
            OpenRouterBackend(
                model=model,
                api_key=cfg.OPENROUTER_API_KEY,
                reasoning_effort=effort,
                purpose=purpose,
            )
            for model in models
        )
    else:
        log.info("OpenRouter-Stufe übersprungen (API-Key oder Modell fehlt)")

    anthropic_model = str(getattr(cfg, "CHAT_ANTHROPIC_MODEL", "claude-haiku-5-5") or "").strip()
    anthropic_key = str(getattr(cfg, "ANTHROPIC_API_KEY", "") or "").strip()
    if anthropic_key and anthropic_model:
        stages.append(ClaudeBackend(model=anthropic_model, purpose=purpose))
    else:
        log.info("Anthropic-Stufe übersprungen (API-Key oder CHAT_ANTHROPIC_MODEL fehlt)")

    plan_model = str(getattr(cfg, "CHAT_CHATGPT_PLAN_MODEL", "gpt-6-luna") or "").strip()
    auth_file = Path(str(getattr(cfg, "CHATGPT_AUTH_FILE", "~/.config/mantis/chatgpt_auth.json"))).expanduser()
    if not getattr(cfg, "CHAT_CHATGPT_PLAN_ENABLED", True):
        log.info("ChatGPT-Abo übersprungen (CHAT_CHATGPT_PLAN_ENABLED=False)")
    elif not plan_model:
        log.info("ChatGPT-Abo übersprungen (CHAT_CHATGPT_PLAN_MODEL ist leer)")
    elif not chatgpt_auth_is_configured(cfg):
        log.info("ChatGPT-Abo übersprungen (kein gültiger Login in CHATGPT_AUTH_FILE)")
    else:
        stages.append(ChatGPTPlanBackend(model=plan_model, auth_file=auth_file, purpose=purpose))
    if purpose == "vision":
        stages = [stage for stage in stages if stage.supports_vision]
    return stages


def cloud_chain(
    purpose: str,
    *,
    reasoning: str | None = None,
    timeout: float | None = None,
    cfg=config,
) -> FallbackBackend:
    """Baut OpenRouter → Anthropic → ChatGPT-Abo ohne lokale LLM-Stufe."""
    stages = _cloud_backends(cfg, purpose, reasoning)
    if not stages:
        raise CloudChainUnavailableError(
            "Kein Cloud-LLM-Anbieter konfiguriert (OpenRouter, Anthropic oder ChatGPT-Abo)."
        )
    stage_timeout = float(
        getattr(cfg, "CHAT_CLOUD_TIMEOUT_S", 30) if timeout is None else timeout
    )
    return FallbackBackend(
        backends=stages,
        stage_timeouts=[stage_timeout] * len(stages),
    )


def build_chat_backend(cfg) -> FallbackBackend:
    """Baut die bestehende Chat-Kette mit Chat-Purpose und bisherigen Modellen."""
    return cloud_chain("chat-agent", cfg=cfg)


def build_chat_task_provider(cfg) -> BackendLLMAdapter:
    """Chat-nahe Hintergrundaufgaben verwenden dieselbe Cloud-Fallback-Kette."""
    return BackendLLMAdapter(cloud_chain("background", cfg=cfg))


def describe_backend_chain(backend) -> str:
    """Liefert eine kompakte Provider-/Modellfolge fürs Startlog."""
    backends = backend.backends if isinstance(backend, FallbackBackend) else (backend,)
    labels = []
    known = {
        "OpenRouterBackend": "OpenRouter",
        "ClaudeBackend": "Anthropic",
        "ChatGPTPlanBackend": "ChatGPT-Abo",
    }
    for stage in backends:
        provider = getattr(stage, "provider_name", None) or known.get(
            type(stage).__name__, type(stage).__name__.removesuffix("Backend")
        )
        labels.append(f"{provider} {stage.model_name}")
    return " → ".join(labels)
