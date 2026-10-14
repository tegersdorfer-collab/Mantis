"""Kurze Klassifikationen und Entscheidungen über die Cloud-Kette."""
import logging

from core.backends.factory import cloud_chain
from core.backends.llm_adapter import BackendLLMAdapter

log = logging.getLogger(__name__)
_provider: BackendLLMAdapter | None = None


def _get_provider() -> BackendLLMAdapter:
    global _provider
    if _provider is None:
        _provider = BackendLLMAdapter(
            cloud_chain("fast", reasoning="minimal", timeout=3, total_timeout=9),
        )
    return _provider


def get_backend_chain():
    """Gibt die initialisierte Fast-Cloud-Kette für das Startlog zurück."""
    return _get_provider().backend


async def ask(
    prompt: str,
    max_tokens: int = 20,
    temperature: float = 0.1,
    format: str | dict | None = None,
) -> str:
    """Gibt bei einem Cloud-Fehler eine leere Antwort zurück."""
    try:
        return (await _get_provider().complete(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            format=format,
        )).strip()
    except Exception as e:
        log.debug("fast.ask fehlgeschlagen: %s", e)
        return ""


async def yes_no(question: str) -> bool:
    out = await ask(f"{question}\n\nAntworte NUR mit JA oder NEIN.", max_tokens=5)
    return out.strip().upper().startswith(("JA", "YES"))
