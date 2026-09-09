"""
RoutedLLMProvider — wählt je nach Aufgabe den optimalen lokalen LLM-Spezialisten.

Routing-Logik (Keyword-basiert, kein extra Router-Modell nötig):
  - Code/Skill-Generierung  → BG_CODE_MODEL    (Qwen2.5-Coder-7B)
  - Briefing/Insights       → BG_REASONING_MODEL (DeepSeek-R1-14B)
  - Alles andere            → BG_DEFAULT_MODEL  (qwen3.5:9b, bleibt Default)

Für den Rest von Mantis ist RoutedLLMProvider ein normaler LLMProvider —
kein Caller muss geändert werden.
"""
import logging
from .base import LLMProvider
from .local import OllamaProvider
import config

log = logging.getLogger(__name__)

# Keywords die auf Reasoning-Modell routen (Briefings, komplexe Synthese)
_REASONING_KEYWORDS = (
    "briefing", "morgen-briefing", "morgenupdate", "abend-review", "wöchentlicher rückblick",
    "pattern", "muster erkenn", "insight", "analyse", "analysiere", "weekly",
    "zusammenfassung der woche", "tages-zusammenfassung", "reflexion",
)

# Keywords die auf Code-Modell routen (Skill-Factory)
_CODE_KEYWORDS = (
    "def ", "async def ", "```python", "import ", "class ",
    "python-skill", "create_skill", "schreibe eine funktion",
    "python funktion", "python-funktion", "schreib python",
    "skill erstellen", "neuen skill", "skill schreiben",
)


def _detect_route(prompt: str) -> str:
    """Gibt 'reasoning', 'code' oder 'default' zurück."""
    lower = prompt.lower()
    if any(k in lower for k in _CODE_KEYWORDS):
        return "code"
    if any(k in lower for k in _REASONING_KEYWORDS):
        return "reasoning"
    return "default"


class RoutedLLMProvider(LLMProvider):
    """bg_llm-Ersatz der transparent zwischen Spezialisten routet."""

    def __init__(
        self,
        default_model:   str | None = None,
        reasoning_model: str | None = None,
        code_model:      str | None = None,
    ):
        self._default_model   = default_model   or getattr(config, "BG_DEFAULT_MODEL",   config.OLLAMA_MODEL)
        self._reasoning_model = reasoning_model or getattr(config, "BG_REASONING_MODEL", "")
        self._code_model      = code_model      or getattr(config, "BG_CODE_MODEL",      "")

        self._providers: dict[str, OllamaProvider] = {
            "default": OllamaProvider(model=self._default_model),
        }
        if self._reasoning_model:
            self._providers["reasoning"] = OllamaProvider(model=self._reasoning_model)
        if self._code_model:
            self._providers["code"] = OllamaProvider(model=self._code_model)

        log.info(
            f"RoutedLLMProvider: default={self._default_model} | "
            f"reasoning={self._reasoning_model or '–'} | "
            f"code={self._code_model or '–'}"
        )

    @property
    def model_name(self) -> str:
        return f"routed({self._default_model})"

    def _route_and_provider(self, prompt: str) -> tuple[str, OllamaProvider]:
        """Gibt die TATSÄCHLICH bediente Route zurück, nicht die erkannte.

        Fehlt der Spezialist (z. B. kein BG_CODE_MODEL gesetzt), landet der
        Prompt beim Default-Modell — dann muss auch 'default' herauskommen,
        sonst bekäme das Default-Modell die Sampling-Defaults eines Coders,
        der gar nicht läuft. Die Temperatur gehört zum Modell, nicht zum Thema.
        """
        erkannt = _detect_route(prompt)
        provider = self._providers.get(erkannt)
        route = erkannt if provider is not None else "default"
        provider = provider or self._providers["default"]
        if route != "default":
            log.debug(f"Route → {route} ({provider._model})")
        return route, provider

    def _pick(self, prompt: str) -> OllamaProvider:
        return self._route_and_provider(prompt)[1]

    def _pick_from_messages(self, messages: list) -> OllamaProvider:
        return self._route_from_messages(messages)[1]

    def _route_from_messages(self, messages: list) -> tuple[str, OllamaProvider]:
        combined = " ".join(
            (m["content"] if isinstance(m, dict) else m.content)
            for m in messages
        )
        return self._route_and_provider(combined)

    @staticmethod
    def _mit_route_defaults(route: str, kwargs: dict) -> dict:
        """Ergänzt die Sampling-Defaults des gewählten Spezialisten.

        Der Ollama-Backend-Pfad schickt `temperature` IMMER explizit mit und
        überschreibt damit still den Wert aus dem Modelfile — ein Modell, das
        für Code 0.6 empfiehlt, läuft sonst auf dem generischen 0.7-Default.
        Gemessen am 09.09.2026 gegen die Code-Aufgabe des Benchmarks:
        0.6 → 8/8 bestanden, 0.7 → 7/8 (kleines n, aber die Richtung deckt sich
        mit der Empfehlung der Ornith-Modellkarte für präzises Coden).

        Ein Caller, der `temperature` selbst setzt, behält Vorrang.
        """
        if route == "code" and "temperature" not in kwargs:
            kwargs = {**kwargs, "temperature": config.BG_CODE_TEMPERATURE}
        return kwargs

    async def chat(self, messages, system=None, **kwargs) -> str:
        route, provider = self._route_from_messages(messages)
        kwargs = self._mit_route_defaults(route, kwargs)
        return await provider.chat(messages, system=system, **kwargs)

    async def complete(self, prompt: str, **kwargs) -> str:
        # Geht über provider.complete() und damit NICHT durch chat() oben —
        # die Defaults müssen hier eigenständig gesetzt werden.
        route, provider = self._route_and_provider(prompt)
        kwargs = self._mit_route_defaults(route, kwargs)
        return await provider.complete(prompt, **kwargs)

    async def stream(self, messages, system=None, **kwargs):
        route, provider = self._route_from_messages(messages)
        kwargs = self._mit_route_defaults(route, kwargs)
        async for chunk in provider.stream(messages, system=system, **kwargs):
            yield chunk

    async def embed(self, text: str) -> list[float]:
        return await self._providers["default"].embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._providers["default"].embed_batch(texts)

    async def pull_if_missing(self) -> None:
        for name, p in self._providers.items():
            await p.pull_if_missing()

    async def warmup(self) -> None:
        """Nur Default vorwärmen — Reasoning/Code werden bei Bedarf geladen."""
        await self._providers["default"].pull_if_missing()
