"""core/decisions.py: Fragen + Schwellen an einer Stelle; Fallback greift bei Ausfall UND bei Unsicherheit."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import patch

from core import decisions
from core.decide import Answer, JevUnavailable


def _noul(p: float) -> Answer:
    return Answer("noul", p >= 0.5, p, abs(p - 0.5) * 2)


async def _fallback_true():
    return True


async def _fallback_false():
    return False


def _jev_returning(answers: dict):
    async def fake(state, questions):
        fake.calls.append((state, questions))
        return {k: v for k, v in answers.items() if k in questions}
    fake.calls = []
    return fake


def _jev_down(state, questions):
    raise JevUnavailable("aus")


def test_addressed_sicher_ja_ohne_fallback():
    fake = _jev_returning({"addressed": _noul(0.97)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.addressed("mach das Licht an", _fallback_false)) is True
    state, qs = fake.calls[0]
    assert state == {"transkript": "mach das Licht an"} and set(qs) == {"addressed"}


def test_addressed_unsicher_nimmt_fallback():
    fake = _jev_returning({"addressed": _noul(0.6)})   # confidence 0.2 < SURE
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.addressed("hm ja", _fallback_true)) is True
        assert asyncio.run(decisions.addressed("hm ja", _fallback_false)) is False


def test_addressed_jev_down_nimmt_fallback():
    with patch.object(decisions.decide, "decide", _jev_down):
        assert asyncio.run(decisions.addressed("x", _fallback_true)) is True


def test_claim_supported_state_nennt_sprecher():
    fake = _jev_returning({"supported": _noul(0.05)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.claim_supported("Ich war beim Zahnarzt", "Timo hat Angst", _fallback_true)) is False
    state, _ = fake.calls[0]
    assert "Timo" in state["sprecher"] and state["text"] == "Ich war beim Zahnarzt" and state["behauptung"] == "Timo hat Angst"


def test_supersedes_state_felder():
    fake = _jev_returning({"supersedes": _noul(0.9)})
    with patch.object(decisions.decide, "decide", fake):
        assert asyncio.run(decisions.supersedes("wohnt in Nürnberg", "nach Fürth gezogen", _fallback_false)) is True
    state, _ = fake.calls[0]
    assert state == {"alte_aussage": "wohnt in Nürnberg", "neue_aussage": "nach Fürth gezogen"}


def test_tool_categories_fanout():
    answers = {f"cat:{c}": _noul(0.02) for c in decisions.TOOL_CATEGORY_DESCRIPTIONS}
    answers["cat:flipper"] = _noul(0.95)
    answers["cat:productivity"] = _noul(0.61)
    answers["aktion"] = _noul(0.9)
    fake = _jev_returning(answers)
    with patch.object(decisions.decide, "decide", fake):
        cats, aktion = asyncio.run(decisions.tool_categories("Lampe an und erinner mich später"))
    assert cats == {"flipper", "productivity"} and aktion is True
    _, qs = fake.calls[0]
    assert "aktion" in qs and len(qs) == len(decisions.TOOL_CATEGORY_DESCRIPTIONS) + 1


def test_tool_categories_aktion_unsicher_ist_none():
    answers = {f"cat:{c}": _noul(0.02) for c in decisions.TOOL_CATEGORY_DESCRIPTIONS}
    answers["aktion"] = _noul(0.55)
    with patch.object(decisions.decide, "decide", _jev_returning(answers)):
        cats, aktion = asyncio.run(decisions.tool_categories("hm"))
    assert cats == set() and aktion is None


def test_tool_categories_jev_down_ist_none():
    with patch.object(decisions.decide, "decide", _jev_down):
        assert asyncio.run(decisions.tool_categories("x")) is None


def test_beschreibungen_decken_registry_kategorien():
    """Jede Tool-Kategorie in der REGISTRY braucht eine Beschreibung, sonst kann Jev sie nie wählen."""
    import core.skills  # noqa: F401  (registriert alle Tools)
    from core.tools import REGISTRY
    fehlt = {t.category for t in REGISTRY.values()} - set(decisions.TOOL_CATEGORY_DESCRIPTIONS) - decisions.TOOL_CATEGORIES_OHNE_ROUTING
    assert not fehlt, f"ohne Beschreibung: {fehlt}"
