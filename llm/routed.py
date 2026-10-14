"""Background-LLM-Adapter mit aufgabenabhängigen Sampling-Defaults.

Alle Routen verwenden dieselbe Cloud-Fallback-Kette. Die Code-Route behält
ihre niedrigere Temperatur; Embeddings laufen separat weiter über Ollama.
"""
import logging

from .base import LLMProvider
from .local import OllamaEmbeddingProvider
import config
from core.backends.factory import cloud_chain, describe_backend_chain
from core.backends.llm_adapter import BackendLLMAdapter

log = logging.getLogger(__name__)

_REASONING_KEYWORDS = (
    "briefing", "morgen-briefing", "morgenupdate", "abend-review", "wöchentlicher rückblick",
    "pattern", "muster erkenn", "insight", "analyse", "analysiere", "weekly",
    "zusammenfassung der woche", "tages-zusammenfassung", "reflexion",
)
_CODE_KEYWORDS = (
    "def ", "async def ", "```python", "import ", "class ",
    "python-skill", "create_skill", "schreibe eine funktion",
    "python funktion", "python-funktion", "schreib python",
    "skill erstellen", "neuen skill", "skill schreiben",
)


def _detect_route(prompt: str) -> str:
    lower = prompt.lower()
    if any(keyword in lower for keyword in _CODE_KEYWORDS):
        return "code"
    if any(keyword in lower for keyword in _REASONING_KEYWORDS):
        return "reasoning"
    return "default"


class RoutedLLMProvider(LLMProvider):
    """LLMProvider-Fassade für Cloud-Background-Aufgaben und lokale Embeddings."""

    def __init__(self, backend=None, embedding_provider=None):
        self._provider = backend or BackendLLMAdapter(
            cloud_chain("background", reasoning="low"),
        )
        self._embed_provider = embedding_provider or OllamaEmbeddingProvider()
        log.info("Background-LLM: %s", self.model_name)

    @property
    def model_name(self) -> str:
        backend = getattr(self._provider, "backend", None)
        if backend is not None:
            return describe_backend_chain(backend)
        return getattr(self._provider, "model_name", "Cloud-LLM")

    def _route(self, prompt: str) -> str:
        route = _detect_route(prompt)
        if route != "default":
            log.debug("Background-Route → %s", route)
        return route

    @staticmethod
    def _mit_route_defaults(route: str, kwargs: dict) -> dict:
        if route == "code" and "temperature" not in kwargs:
            return {**kwargs, "temperature": config.BG_CODE_TEMPERATURE}
        return kwargs

    def _prompt_from_messages(self, messages: list) -> str:
        return " ".join(
            (message["content"] if isinstance(message, dict) else message.content)
            for message in messages
        )

    async def chat(self, messages, system=None, **kwargs) -> str:
        route = self._route(self._prompt_from_messages(messages))
        return await self._provider.chat(
            messages,
            system=system,
            **self._mit_route_defaults(route, kwargs),
        )

    async def complete(self, prompt: str, **kwargs) -> str:
        route = self._route(prompt)
        return await self._provider.complete(prompt, **self._mit_route_defaults(route, kwargs))

    async def stream(self, messages, system=None, **kwargs):
        route = self._route(self._prompt_from_messages(messages))
        async for chunk in self._provider.stream(
            messages,
            system=system,
            **self._mit_route_defaults(route, kwargs),
        ):
            yield chunk

    async def embed(self, text: str) -> list[float]:
        return await self._embed_provider.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._embed_provider.embed_batch(texts)

    async def warmup(self) -> None:
        return None
