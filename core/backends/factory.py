"""Konfiguration und Aufbau der Chat-Backend-Ketten.

Datenschutzgrenze (Entscheidung Timo, 13.10.2026): Der Chat-Turn darf den vollen
Kontext an die Cloud senden, inkl. System-Prompt mit LZG, Aufgaben, Profil und
Health/COROS sowie wiederhergestellter KZG-Historie. Dashboard-/Background-Routen
(Journal, Aufgaben, Gesundheit, Fitness) verarbeiten gespeicherte Daten dagegen
ausschließlich lokal über bg_llm.
"""
from core.backends.claude import ClaudeBackend
from core.backends.fallback import FallbackBackend
from core.backends.llm_adapter import BackendLLMAdapter
from core.backends.ollama import OllamaBackend
from core.backends.openrouter import OpenRouterBackend


def _openrouter_models(cfg) -> list[str]:
    if getattr(cfg, "LLM_LOCAL_ONLY", False):
        return []
    if not getattr(cfg, "OPENROUTER_API_KEY", ""):
        return []
    primary = str(getattr(cfg, "CHAT_CLOUD_MODEL", "") or "").strip()
    if not primary:
        return []
    models = [primary]
    fallback = str(getattr(cfg, "CHAT_CLOUD_FALLBACK_MODEL", "") or "").strip()
    if fallback and fallback != primary:
        models.append(fallback)
    return models


def _openrouter_chain(cfg, purpose: str, local_backend=None) -> FallbackBackend | None:
    models = _openrouter_models(cfg)
    if not models:
        return None
    cloud_backends = [
        OpenRouterBackend(
            model=model,
            api_key=cfg.OPENROUTER_API_KEY,
            reasoning_effort=cfg.CHAT_CLOUD_REASONING_EFFORT,
            purpose=purpose,
        )
        for model in models
    ]
    local = local_backend if local_backend is not None else OllamaBackend()
    timeout = cfg.CHAT_CLOUD_TIMEOUT_S
    return FallbackBackend(
        backends=[*cloud_backends, local],
        stage_timeouts=[timeout] * len(cloud_backends) + [None],
    )


def build_chat_backend(cfg) -> FallbackBackend | OllamaBackend:
    """Baut die Chat-Kette und erhält die bisherige Claude-Auswahl als Fallback."""
    cloud = _openrouter_chain(cfg, purpose="chat-agent")
    if cloud is not None:
        return cloud

    if not getattr(cfg, "LLM_LOCAL_ONLY", False) and getattr(cfg, "ANTHROPIC_API_KEY", ""):
        return FallbackBackend(
            primary=ClaudeBackend(model=cfg.CLAUDE_CHAT_MODEL),
            fallback=OllamaBackend(),
        )

    return OllamaBackend(model=cfg.AGENT_MODEL_STRONG, keep_alive="-1")


def build_chat_task_provider(cfg) -> BackendLLMAdapter | None:
    """Cloud-Kette nur für Chat-Turn-Fact-Extraction und KZG-Summary bereitstellen."""
    if not getattr(cfg, "BG_CHAT_TASKS_USE_CLOUD", True):
        return None
    chain = _openrouter_chain(cfg, purpose="background")
    return BackendLLMAdapter(chain) if chain is not None else None


def describe_backend_chain(backend) -> str:
    """Liefert eine kompakte Provider-/Modellfolge fürs Startlog."""
    backends = backend.backends if isinstance(backend, FallbackBackend) else (backend,)
    labels = []
    known = {
        "OpenRouterBackend": "OpenRouter",
        "ClaudeBackend": "Claude",
        "OllamaBackend": "Ollama",
    }
    for stage in backends:
        provider = getattr(stage, "provider_name", None) or known.get(
            type(stage).__name__, type(stage).__name__.removesuffix("Backend")
        )
        labels.append(f"{provider} {stage.model_name}")
    return " → ".join(labels)
