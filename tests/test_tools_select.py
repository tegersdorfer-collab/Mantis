"""select_tools_async: Jev-Kategorien erweitern den Keyword-Pfad, ersetzen ihn nicht."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, patch

import core.skills  # noqa: F401  registriert alle Tools
from core import tools as T


def test_ohne_jev_identisch_zum_sync_pfad():
    text = "Erinnere mich morgen um 9 an den Zahnarzt"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=None)):
        names, force = asyncio.run(T.select_tools_async(text))
    assert names == T.select_tools(text)
    assert force == (bool(names) and T.is_action(text))


def test_jev_fuegt_kategorie_hinzu_die_keywords_verpassen():
    text = "spiel was von billie eilish"   # kein Keyword für spotify in der Liste? egal — wir zwingen die Kategorie
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=({"spotify"}, True))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert "spotify" in names
    assert force is True
    # Keyword-Ergebnis bleibt enthalten
    for n in T.select_tools(text):
        assert n in names


def test_jev_aktion_unsicher_laesst_keyword_entscheiden():
    text = "Leg eine Aufgabe an: Steuer machen"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=(set(), None))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert force == (bool(names) and T.is_action(text))


def test_jev_aktion_false_ueberstimmt_keyword_nicht():
    """Jev darf Tool-Calls nicht abschalten, wenn ein Aktionswort da ist — Recall vor Precision."""
    text = "Leg eine Aufgabe an: Steuer machen"
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=({"productivity"}, False))):
        names, force = asyncio.run(T.select_tools_async(text))
    assert force is True


def test_deckel_bleibt():
    alle = set(T.decisions.TOOL_CATEGORY_DESCRIPTIONS)
    with patch("core.tools.decisions.tool_categories", new=AsyncMock(return_value=(alle, True))):
        names, _ = asyncio.run(T.select_tools_async("alles auf einmal"))
    assert len(names) <= 14 + 3   # 14 Prefill-Deckel + die 3 Skill-Factory-Tools
