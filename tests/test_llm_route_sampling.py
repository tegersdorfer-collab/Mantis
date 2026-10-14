"""Regressionstests für die Sampling-Defaults der Code-Route in der Cloud-Kette.

Zweite Falle, die hier festgenagelt wird: `complete()` läuft über
`provider.complete()` und damit NICHT durch `RoutedLLMProvider.chat()`. Ein
Default, der nur in chat() gesetzt wird, greift auf dem Pfad nicht — genau den
benutzen core/idle_loop.py und domains/second_brain.py.

Kein Netzwerk: chat()/stream() werden durchgehend gestubbt.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.backends.base import AgentBackend
from llm.routed import RoutedLLMProvider

CODE_PROMPT = "Schreibe eine Python-Funktion die JSON flacht"
DEFAULT_PROMPT = "Wie war mein Schlaf letzte Nacht?"
REASONING_PROMPT = "Erstelle das Morgen-Briefing für heute"


def _routed():
    class Provider:
        async def chat(self, messages, system=None, **kwargs):
            return "ok"

        async def complete(self, prompt, system=None, **kwargs):
            from llm.base import Message
            return await self.chat([Message(role="user", content=prompt)], system=system, **kwargs)

        async def stream(self, messages, system=None, **kwargs):
            yield "a"
            yield "b"

        async def embed(self, text):
            return []

    return RoutedLLMProvider(backend=Provider(), embedding_provider=object())


def _stub_chat(provider, recorder):
    async def fake_chat(messages, system=None, **kwargs):
        recorder.append(kwargs)
        return "ok"

    provider.chat = fake_chat
    return recorder


def _stub_stream(provider, recorder):
    async def fake_stream(messages, system=None, **kwargs):
        recorder.append(kwargs)
        for chunk in ("a", "b"):
            yield chunk

    provider.stream = fake_stream
    return recorder


# ── chat() ────────────────────────────────────────────────────────────────────

def test_code_route_bekommt_bg_code_temperature():
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}]))

    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


def test_expliziter_caller_wert_schlaegt_den_default():
    """Ein Caller, der bewusst eine Temperatur setzt, darf nicht überstimmt werden."""
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}], temperature=0.95))

    assert calls[0]["temperature"] == 0.95


def test_default_route_bleibt_unberuehrt():
    """Nur die Code-Route bekommt den Sonderwert — sonst nichts."""
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.chat([{"role": "user", "content": DEFAULT_PROMPT}]))

    assert "temperature" not in calls[0]


def test_reasoning_route_bleibt_unberuehrt():
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.chat([{"role": "user", "content": REASONING_PROMPT}]))

    assert "temperature" not in calls[0]


# ── complete() — der Pfad, der an chat() vorbeiläuft ──────────────────────────

def test_complete_setzt_die_code_temperatur_ebenfalls():
    """complete() delegiert an provider.complete(), nicht an Routed.chat().

    Genau hier würde ein nur in chat() gesetzter Default stillschweigend fehlen.
    """
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.complete(CODE_PROMPT))

    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


def test_complete_respektiert_expliziten_wert():
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.complete(CODE_PROMPT, temperature=0.95))

    assert calls[0]["temperature"] == 0.95


def test_complete_auf_default_route_ohne_sonderwert():
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.complete(DEFAULT_PROMPT, max_tokens=300))

    assert "temperature" not in calls[0]
    assert calls[0]["max_tokens"] == 300


# ── stream() ──────────────────────────────────────────────────────────────────

def test_stream_setzt_die_code_temperatur():
    r = _routed()
    calls = _stub_stream(r._provider, [])

    async def lauf():
        return [c async for c in r.stream([{"role": "user", "content": CODE_PROMPT}])]

    assert asyncio.run(lauf()) == ["a", "b"]
    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


# ── Konfiguration ─────────────────────────────────────────────────────────────

def test_bg_code_temperature_ist_konfiguriert_und_plausibel():
    assert isinstance(config.BG_CODE_TEMPERATURE, float)
    assert 0.0 <= config.BG_CODE_TEMPERATURE <= 1.0


def test_code_route_bleibt_auf_dem_cloud_modell_mit_code_sampling():
    r = _routed()
    calls = _stub_chat(r._provider, [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}]))

    assert len(calls) == 1
    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


def test_routed_provider_uses_cloud_background_chain_without_local_provider(monkeypatch):
    import llm.routed as routed

    cloud_calls = []
    local_instances = []

    class CloudBackend(AgentBackend):
        @property
        def model_name(self):
            return "fake-cloud"

        async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                       think=False, force_tool_call=False):
            cloud_calls.append({"messages": messages, "temperature": temperature})
            return "cloud answer", []

        async def warmup(self):
            return None

    class EmbeddingProvider:
        def __init__(self, *args, **kwargs):
            local_instances.append((args, kwargs))

        async def embed(self, text):
            raise AssertionError("Der Embedding-Provider darf keine Textgenerierung übernehmen")

        async def embed_batch(self, texts):
            raise AssertionError("Der Embedding-Provider darf keine Textgenerierung übernehmen")

    def cloud_chain(purpose, **kwargs):
        cloud_calls.append((purpose, kwargs))
        return CloudBackend()

    monkeypatch.setattr(routed, "cloud_chain", cloud_chain, raising=False)
    monkeypatch.setattr(routed, "OllamaEmbeddingProvider", EmbeddingProvider)

    provider = RoutedLLMProvider()
    result = asyncio.run(provider.complete(CODE_PROMPT))

    assert result == "cloud answer"
    assert cloud_calls[0] == ("background", {"reasoning": "low"})
    assert cloud_calls[1]["temperature"] == config.BG_CODE_TEMPERATURE
    assert local_instances == [((), {})]  # Der lokale Provider bleibt Embeddings vorbehalten.
