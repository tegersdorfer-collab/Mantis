"""Extractor-Verifier & Konflikt-Judge gehen über Jev; lokal ist Fallback — und der lokale
Prompt nennt jetzt den Sprecher (Text „ich" ≠ Behauptung „Timo" war ein stiller Ablehnungsgrund)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, patch

from memory import extractor as ex


def test_verify_claim_fragt_jev_mit_fallback():
    with patch("memory.extractor.decisions.claim_supported", new=AsyncMock(return_value=False)) as jev:
        ok = asyncio.run(ex.verify_claim(object(), "Ich war beim Zahnarzt", "Timo hat Angst"))
    assert ok is False
    text, claim, fallback = jev.await_args.args
    assert (text, claim) == ("Ich war beim Zahnarzt", "Timo hat Angst") and callable(fallback)


def test_lokaler_verifier_prompt_nennt_sprecher():
    prompt = ex.verify_prompt("Ich war beim Zahnarzt", "Timo war beim Zahnarzt")
    assert "Timo" in prompt and "Ich-Form" in prompt
    assert prompt.startswith("Antworte NUR mit JA oder NEIN")


def test_lokaler_verifier_fallback_parst_ja():
    class Msg:  content = "JA"
    class Resp: message = Msg()
    client = type("C", (), {"chat": AsyncMock(return_value=Resp())})()

    async def _rufe_fallback(t, c, fb):
        # AsyncMock awaited einen sync-lambda-side_effect nicht selbst (fb() bliebe
        # ein ungestartetes Coroutine-Objekt) — darum hier ein async side_effect.
        return await fb()

    with patch("memory.extractor.decisions.claim_supported",
               new=AsyncMock(side_effect=_rufe_fallback)):
        assert asyncio.run(ex.verify_claim(client, "Ich war beim Zahnarzt", "Timo war beim Zahnarzt")) is True
    assert client.chat.await_args.kwargs["model"] == "qwen2.5:0.5b"


def test_judge_geht_ueber_jev():
    with patch("memory.extractor.decisions.supersedes", new=AsyncMock(return_value=True)) as jev:
        judge = ex.make_judge(object(), "qwen3.5:9b")
        assert asyncio.run(judge("wohnt in Nürnberg", "nach Fürth gezogen")) is True
    old, new, fallback = jev.await_args.args
    assert (old, new) == ("wohnt in Nürnberg", "nach Fürth gezogen") and callable(fallback)
