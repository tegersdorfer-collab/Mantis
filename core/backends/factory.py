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
from core.backends.gemini import GeminiBackend
from core.backends.llm_adapter import BackendLLMAdapter
from core.backends.openrouter import OpenRouterBackend

log = logging.getLogger(__name__)

DEFAULT_CLOUD_CHAIN_ORDER = ("openrouter", "anthropic", "chatgpt-plan")


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
    stage_groups = {"openrouter": [], "anthropic": [], "chatgpt-plan": [], "gemini": []}
    models = _openrouter_models(cfg)
    if purpose != "chat-agent":
        models = models[:1]
    if models:
        effort = "minimal" if purpose == "fast" else (
            reasoning if reasoning is not None else getattr(cfg, "CHAT_CLOUD_REASONING_EFFORT", "low")
        )
        stage_groups["openrouter"].extend(
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
        stage_groups["anthropic"].append(ClaudeBackend(model=anthropic_model, purpose=purpose))
    else:
        log.info("Anthropic-Stufe übersprungen (API-Key oder CHAT_ANTHROPIC_MODEL fehlt)")

    gemini_model = str(getattr(cfg, "GEMINI_MODEL", "gemini-3-flash") or "").strip()
    gemini_key = str(getattr(cfg, "GEMINI_API_KEY", "") or "").strip()
    if gemini_key and gemini_model:
        stage_groups["gemini"].append(
            GeminiBackend(model=gemini_model, api_key=gemini_key, purpose=purpose)
        )
    else:
        log.info("Gemini-Stufe übersprungen (GEMINI_API_KEY oder GEMINI_MODEL fehlt)")

    plan_model = str(getattr(cfg, "CHAT_CHATGPT_PLAN_MODEL", "gpt-6-luna") or "").strip()
    auth_file = Path(str(getattr(cfg, "CHATGPT_AUTH_FILE", "~/.config/mantis/chatgpt_auth.json"))).expanduser()
    if not getattr(cfg, "CHAT_CHATGPT_PLAN_ENABLED", True):
        log.info("ChatGPT-Abo übersprungen (CHAT_CHATGPT_PLAN_ENABLED=False)")
    elif not plan_model:
        log.info("ChatGPT-Abo übersprungen (CHAT_CHATGPT_PLAN_MODEL ist leer)")
    elif not chatgpt_auth_is_configured(cfg):
        log.info("ChatGPT-Abo übersprungen (kein gültiger Login in CHATGPT_AUTH_FILE)")
    else:
        stage_groups["chatgpt-plan"].append(
            ChatGPTPlanBackend(model=plan_model, auth_file=auth_file, purpose=purpose)
        )

    order_setting = {
        "chat-agent": "CHAT_AGENT_CHAIN_ORDER",
        "fast": "FAST_CHAIN_ORDER",
        "voice": "VOICE_CHAIN_ORDER",
        "background": "BACKGROUND_CHAIN_ORDER",
        "vision": "VISION_CHAIN_ORDER",
    }.get(purpose)
    order = getattr(cfg, order_setting, DEFAULT_CLOUD_CHAIN_ORDER) if order_setting else DEFAULT_CLOUD_CHAIN_ORDER
    if isinstance(order, str):
        raise ValueError(f"{order_setting} muss eine Liste von Cloud-Stufen sein")
    order = tuple(str(stage).strip().casefold() for stage in order)
    allowed = set(stage_groups)
    if not order or any(stage not in allowed for stage in order) or len(order) != len(set(order)):
        raise ValueError(f"Ungültige Cloud-Kettenreihenfolge für Zweck {purpose!r}")
    stages = [backend for stage in order for backend in stage_groups[stage]]
    if purpose == "vision":
        stages = [stage for stage in stages if stage.supports_vision]
    return stages


def cloud_chain(
    purpose: str,
    *,
    reasoning: str | None = None,
    timeout: float | None = None,
    total_timeout: float | None = None,
    cfg=config,
) -> FallbackBackend:
    """Baut die konfigurierte Cloud-Kette ohne lokale LLM-Stufe."""
    stages = _cloud_backends(cfg, purpose, reasoning)
    if not stages:
        raise CloudChainUnavailableError(
            "Kein Cloud-LLM-Anbieter konfiguriert (OpenRouter, Anthropic, ChatGPT-Abo oder Gemini)."
        )
    if timeout is None:
        configured_stage_timeout = float(getattr(cfg, "CHAT_CLOUD_TIMEOUT_S", 30))
        stage_timeout = (
            min(configured_stage_timeout, float(total_timeout) / len(stages))
            if total_timeout is not None else configured_stage_timeout
        )
    else:
        stage_timeout = float(timeout)
    return FallbackBackend(
        backends=stages,
        stage_timeouts=[stage_timeout] * len(stages),
        total_timeout_s=total_timeout,
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
