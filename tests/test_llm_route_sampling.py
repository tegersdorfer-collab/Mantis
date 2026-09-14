"""Regressionstests für die Sampling-Defaults der Code-Route.

Hintergrund: Der Ollama-Backend-Pfad schickt `temperature` IMMER explizit mit
(core/backends/ollama.py, llm/local.py) und überschreibt damit still den Wert
aus dem Modelfile des Modells. `ornith-1.5:9b-sys` bringt temperature 0.6 mit,
lief in Mantis aber auf dem generischen 0.7-Default. Gemessen am 09.09.2026
gegen die Code-Aufgabe des Benchmarks: 0.6 → 8/8 bestanden, 0.7 → 7/8.

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
from llm.routed import RoutedLLMProvider

CODE_PROMPT = "Schreibe eine Python-Funktion die JSON flacht"
DEFAULT_PROMPT = "Wie war mein Schlaf letzte Nacht?"
REASONING_PROMPT = "Erstelle das Morgen-Briefing für heute"


def _routed():
    return RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")


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
    calls = _stub_chat(r._providers["code"], [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}]))

    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


def test_expliziter_caller_wert_schlaegt_den_default():
    """Ein Caller, der bewusst eine Temperatur setzt, darf nicht überstimmt werden."""
    r = _routed()
    calls = _stub_chat(r._providers["code"], [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}], temperature=0.95))

    assert calls[0]["temperature"] == 0.95


def test_default_route_bleibt_unberuehrt():
    """Nur die Code-Route bekommt den Sonderwert — sonst nichts."""
    r = _routed()
    calls = _stub_chat(r._providers["default"], [])

    asyncio.run(r.chat([{"role": "user", "content": DEFAULT_PROMPT}]))

    assert "temperature" not in calls[0]


def test_reasoning_route_bleibt_unberuehrt():
    r = _routed()
    calls = _stub_chat(r._providers["reasoning"], [])

    asyncio.run(r.chat([{"role": "user", "content": REASONING_PROMPT}]))

    assert "temperature" not in calls[0]


# ── complete() — der Pfad, der an chat() vorbeiläuft ──────────────────────────

def test_complete_setzt_die_code_temperatur_ebenfalls():
    """complete() delegiert an provider.complete(), nicht an Routed.chat().

    Genau hier würde ein nur in chat() gesetzter Default stillschweigend fehlen.
    """
    r = _routed()
    calls = _stub_chat(r._providers["code"], [])

    asyncio.run(r.complete(CODE_PROMPT))

    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


def test_complete_respektiert_expliziten_wert():
    r = _routed()
    calls = _stub_chat(r._providers["code"], [])

    asyncio.run(r.complete(CODE_PROMPT, temperature=0.95))

    assert calls[0]["temperature"] == 0.95


def test_complete_auf_default_route_ohne_sonderwert():
    r = _routed()
    calls = _stub_chat(r._providers["default"], [])

    asyncio.run(r.complete(DEFAULT_PROMPT, max_tokens=300))

    assert "temperature" not in calls[0]
    assert calls[0]["max_tokens"] == 300


# ── stream() ──────────────────────────────────────────────────────────────────

def test_stream_setzt_die_code_temperatur():
    r = _routed()
    calls = _stub_stream(r._providers["code"], [])

    async def lauf():
        return [c async for c in r.stream([{"role": "user", "content": CODE_PROMPT}])]

    assert asyncio.run(lauf()) == ["a", "b"]
    assert calls[0]["temperature"] == config.BG_CODE_TEMPERATURE


# ── Konfiguration ─────────────────────────────────────────────────────────────

def test_bg_code_temperature_ist_konfiguriert_und_plausibel():
    assert isinstance(config.BG_CODE_TEMPERATURE, float)
    assert 0.0 <= config.BG_CODE_TEMPERATURE <= 1.0


def test_fallback_ohne_code_provider_setzt_keinen_sonderwert():
    """Ohne BG_CODE_MODEL landet ein Code-Prompt beim Default-Modell.

    Dessen Temperatur darf dann nicht die des Coders sein — der Wert gilt dem
    Modell, nicht dem Thema.
    """
    r = _routed()
    r._providers.pop("code", None)
    calls = _stub_chat(r._providers["default"], [])

    asyncio.run(r.chat([{"role": "user", "content": CODE_PROMPT}]))

    assert len(calls) == 1
    assert "temperature" not in calls[0]
