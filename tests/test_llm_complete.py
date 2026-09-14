"""Regressionstests für complete() im LLM-Interface.

Hintergrund: RoutedLLMProvider.complete() delegierte an provider.complete(),
aber KEIN konkreter Provider hatte je ein complete() — weder OllamaProvider
noch ClaudeProvider/ClaudeCodeProvider, und base.LLMProvider deklarierte es
auch nicht. Jeder Aufruf endete in AttributeError, der an beiden echten
Aufrufstellen (core/idle_loop.py KZG-Checkpoint, domains/second_brain.py
Inbox-Sorting) still weggeloggt wurde.

Kein Netzwerk: chat() wird durchgehend gestubbt.
"""

import asyncio
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.base import LLMProvider, Message
from llm.local import OllamaProvider
from llm.routed import RoutedLLMProvider


def _stub_chat(provider, recorder):
    """Ersetzt provider.chat durch einen Rekorder — kein Ollama-Call."""

    async def fake_chat(messages, system=None, **kwargs):
        recorder.append({"messages": messages, "system": system, "kwargs": kwargs})
        return "  Antwort  "

    provider.chat = fake_chat
    return recorder


# ── OllamaProvider.complete ───────────────────────────────────────────────────

def test_ollama_complete_exists():
    """Der eigentliche Bug: OllamaProvider hatte kein complete()."""
    assert callable(getattr(OllamaProvider, "complete", None))


def test_ollama_complete_wraps_prompt_as_single_user_message():
    p = OllamaProvider(model="test:1b", embed_model="emb:1b")
    calls = _stub_chat(p, [])

    out = asyncio.run(p.complete("hallo"))

    assert out == "  Antwort  "
    assert len(calls) == 1
    msgs = calls[0]["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], Message)
    assert msgs[0].role == "user"
    assert msgs[0].content == "hallo"


def test_ollama_complete_forwards_kwargs_and_system():
    """max_tokens ist das, was idle_loop/second_brain tatsächlich mitgeben."""
    p = OllamaProvider(model="test:1b", embed_model="emb:1b")
    calls = _stub_chat(p, [])

    asyncio.run(p.complete("hallo", system="Du bist Mantis", max_tokens=300, temperature=0.2))

    assert calls[0]["system"] == "Du bist Mantis"
    assert calls[0]["kwargs"]["max_tokens"] == 300
    assert calls[0]["kwargs"]["temperature"] == 0.2


# ── RoutedLLMProvider.complete ────────────────────────────────────────────────

def test_routed_complete_delegates_to_default_provider():
    """Der exakte reproduzierte Fall: RoutedLLMProvider().complete('hallo')."""
    r = RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")
    calls = _stub_chat(r._providers["default"], [])

    out = asyncio.run(r.complete("hallo"))

    assert out == "  Antwort  "
    assert len(calls) == 1
    assert calls[0]["messages"][0].content == "hallo"


def test_routed_complete_kzg_prompt_uses_default_not_reasoning():
    """KZG-Checkpoint-Prompt aus core/idle_loop.py, inkl. max_tokens=300."""
    r = RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")
    default_calls = _stub_chat(r._providers["default"], [])
    reasoning_calls = _stub_chat(r._providers["reasoning"], [])

    prompt = (
        "Fasse das folgende Gespräch prägnant auf Deutsch zusammen (max 200 Wörter). "
        "Behalte wichtige Fakten, Entscheidungen und Aufgaben.\n\nTimo: hi"
    )
    asyncio.run(r.complete(prompt, max_tokens=300))

    assert len(default_calls) == 1
    assert default_calls[0]["kwargs"]["max_tokens"] == 300
    assert reasoning_calls == []


def test_routed_complete_routes_code_prompt_to_code_provider():
    r = RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")
    default_calls = _stub_chat(r._providers["default"], [])
    code_calls = _stub_chat(r._providers["code"], [])

    asyncio.run(r.complete("Schreibe eine Funktion: async def foo(): ..."))

    assert len(code_calls) == 1
    assert default_calls == []


def test_routed_complete_routes_reasoning_prompt_to_reasoning_provider():
    r = RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")
    default_calls = _stub_chat(r._providers["default"], [])
    reasoning_calls = _stub_chat(r._providers["reasoning"], [])

    asyncio.run(r.complete("Erstelle das Morgen-Briefing für heute"))

    assert len(reasoning_calls) == 1
    assert default_calls == []


def test_routed_complete_falls_back_when_route_provider_missing():
    """Ohne BG_CODE_MODEL existiert kein 'code'-Provider → Default muss greifen.

    (Leere Strings im Konstruktor greifen nicht: `code_model or config...`
    fällt bei "" auf die Config zurück. Deshalb den Provider direkt entfernen.)
    """
    r = RoutedLLMProvider(default_model="d:1b", reasoning_model="r:1b", code_model="c:1b")
    r._providers.pop("code", None)
    default_calls = _stub_chat(r._providers["default"], [])

    asyncio.run(r.complete("Schreibe eine Funktion: async def foo(): ..."))

    assert len(default_calls) == 1


# ── Interface-Lücke insgesamt ─────────────────────────────────────────────────

def test_complete_is_part_of_the_provider_interface():
    """complete() gehört ins Basis-Interface, sonst reißt die Lücke erneut auf."""
    assert callable(getattr(LLMProvider, "complete", None))


def test_all_concrete_providers_inherit_complete():
    """ClaudeProvider/ClaudeCodeProvider hatten complete() ebenfalls nie."""
    from llm.claude import ClaudeProvider
    from llm.claude_code import ClaudeCodeProvider

    for cls in (OllamaProvider, ClaudeProvider, ClaudeCodeProvider, RoutedLLMProvider):
        assert callable(getattr(cls, "complete", None)), f"{cls.__name__} hat kein complete()"
