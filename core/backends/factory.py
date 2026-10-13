"""Konfiguration und Aufbau der Chat-Backend-Ketten.

Datenschutzgrenze (Entscheidung Timo, 13.10.2026): Der Chat-Turn darf den vollen
Kontext an die Cloud senden, inkl. System-Prompt mit LZG, Aufgaben, Profil und
Health/COROS sowie wiederhergestellter KZG-Historie. Dashboard-/Background-Routen
(Journal, Aufgaben, Gesundheit, Fitness) verarbeiten gespeicherte Daten dagegen
ausschließlich lokal über bg_llm.
"""
import logging
from pathlib import Path

from core.auth.chatgpt import is_auth_configured
from core.backends.chatgpt_plan import ChatGPTPlanBackend
from core.backends.claude import ClaudeBackend
from core.backends.fallback import FallbackBackend
from core.backends.llm_adapter import BackendLLMAdapter
from core.backends.ollama import OllamaBackend
from core.backends.openrouter import OpenRouterBackend

log = logging.getLogger(__name__)


def _openrouter_models(cfg) -> list[str]:
    if getattr(cfg, "LLM_LOCAL_ONLY", False):
        return []
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


def _cloud_backends(cfg, purpose: str) -> list:
    if getattr(cfg, "LLM_LOCAL_ONLY", False):
        log.info("Cloud-Chat-Stufen übersprungen (LLM_LOCAL_ONLY=True)")
        return []

    stages = []
    models = _openrouter_models(cfg)
    if models:
        stages.extend(
            OpenRouterBackend(
                model=model,
                api_key=cfg.OPENROUTER_API_KEY,
                reasoning_effort=getattr(cfg, "CHAT_CLOUD_REASONING_EFFORT", "low"),
                purpose=purpose,
            )
            for model in models
        )
    else:
        log.info("OpenRouter-Stufen übersprungen (API-Key oder Modell fehlt)")

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
    return stages


def _chat_chain(cfg, purpose: str) -> FallbackBackend | None:
    cloud_backends = _cloud_backends(cfg, purpose)
    if not cloud_backends:
        return None
    local = OllamaBackend()
    timeout = float(getattr(cfg, "CHAT_CLOUD_TIMEOUT_S", 30))
    return FallbackBackend(
        backends=[*cloud_backends, local],
        stage_timeouts=[timeout] * len(cloud_backends) + [None],
    )


def build_chat_backend(cfg) -> FallbackBackend | OllamaBackend:
    """Baut OpenRouter → Anthropic → ChatGPT-Abo → Ollama gemäß verfügbarer Konfiguration."""
    chain = _chat_chain(cfg, purpose="chat-agent")
    if chain is not None:
        return chain
    return OllamaBackend(model=cfg.AGENT_MODEL_STRONG, keep_alive="-1")


def build_chat_task_provider(cfg) -> BackendLLMAdapter | None:
    """Chat-nahe Hintergrundaufgaben verwenden dieselbe Cloud-Kette mit lokalem Fallback."""
    if not getattr(cfg, "BG_CHAT_TASKS_USE_CLOUD", True):
        return None
    chain = _chat_chain(cfg, purpose="background")
    return BackendLLMAdapter(chain) if chain is not None else None


def describe_backend_chain(backend) -> str:
    """Liefert eine kompakte Provider-/Modellfolge fürs Startlog."""
    backends = backend.backends if isinstance(backend, FallbackBackend) else (backend,)
    labels = []
    known = {
        "OpenRouterBackend": "OpenRouter",
        "ClaudeBackend": "Anthropic",
        "ChatGPTPlanBackend": "ChatGPT-Abo",
        "OllamaBackend": "Ollama",
    }
    for stage in backends:
        provider = getattr(stage, "provider_name", None) or known.get(
            type(stage).__name__, type(stage).__name__.removesuffix("Backend")
        )
        labels.append(f"{provider} {stage.model_name}")
    return " → ".join(labels)
