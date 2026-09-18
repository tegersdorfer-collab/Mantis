"""core/decide.py: Jev-HTTP-Engine mit Circuit-Breaker. Kein Netz, alles per MockTransport."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from core import decide
from core.decide import Noul, Choice, Answer, JevUnavailable


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok_response(request):
    body = json.loads(request.content)
    assert body["model"] == "~typesafe/jev-latest"
    assert request.headers["authorization"] == "Bearer sk-test"
    answers = {}
    for qid, q in body["questions"].items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.93}
        else:
            answers[qid] = {"type": "choice", "choice": "flipper",
                            "probabilities": {"flipper": 0.9, "robot": 0.1}, "confidence": 0.88}
    return httpx.Response(200, json={"model": "typesafe/jev-1.13", "answers": answers,
                                     "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.0}})


def _enabled(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "sk-test")
    decide.reset_for_tests()


def test_disabled_ohne_key(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "")
    decide.reset_for_tests()
    assert decide.enabled() is False
    with pytest.raises(JevUnavailable):
        asyncio.run(decide.decide("x", {"q": Noul("Ist x?")}))


def test_noul_und_choice_werden_typisiert(monkeypatch):
    _enabled(monkeypatch)
    with patch.object(decide, "_transport", _transport(_ok_response)):
        out = asyncio.run(decide.decide({"t": "Licht an"}, {
            "a": Noul("Aktion?", criteria={"true": "ja", "false": "nein"}),
            "k": Choice("Kategorie?", {"flipper": "Lampe", "robot": None}),
        }))
    a, k = out["a"], out["k"]
    assert a.kind == "noul" and a.value is True and a.p == 0.93
    assert abs(a.confidence - 0.86) < 1e-9 and a.sure() is True
    assert k.kind == "choice" and k.value == "flipper" and k.p == 0.9 and k.confidence == 0.88
    assert k.probabilities == {"flipper": 0.9, "robot": 0.1}


def test_noul_unsicher_nahe_0_5(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {"q": {"type": "noul", "noul": 0.6}},
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})
    with patch.object(decide, "_transport", _transport(h)):
        a = asyncio.run(decide.decide("x", {"q": Noul("?")}))["q"]
    assert a.value is True and abs(a.confidence - 0.2) < 1e-9 and a.sure() is False


def test_http_fehler_wirft_und_oeffnet_breaker(monkeypatch):
    _enabled(monkeypatch)
    calls = {"n": 0}
    def h(request):
        calls["n"] += 1
        return httpx.Response(520, text="cloudflare")
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
        # Breaker offen: zweiter Call geht gar nicht mehr raus
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert calls["n"] == 1
    assert decide.enabled() is False   # während Cooldown "nicht verfügbar"


def test_breaker_schliesst_nach_cooldown(monkeypatch):
    _enabled(monkeypatch)
    monkeypatch.setattr(decide.config, "JEV_COOLDOWN_S", 0.0)
    state = {"fail": True}
    def h(request):
        if state["fail"]:
            return httpx.Response(503)
        return _ok_response(request)
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
        state["fail"] = False
        out = asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert out["q"].value is True


def test_timeout_wird_zu_unavailable(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        raise httpx.ReadTimeout("langsam")
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))


def test_fehlende_antwort_id_wirft(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))


def test_kaputte_antwortform_wird_unavailable(monkeypatch):
    _enabled(monkeypatch)
    def h_string(request):
        return httpx.Response(200, json={"model": "m", "answers": {"q": "kaputt"}, "usage": {}})
    with patch.object(decide, "_transport", _transport(h_string)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
    decide.reset_for_tests()
    def h_list(request):
        return httpx.Response(200, json={"model": "m", "answers": {
            "q": {"type": "choice", "choice": "flipper", "probabilities": [0.9, 0.1], "confidence": 0.9}},
            "usage": {}})
    with patch.object(decide, "_transport", _transport(h_list)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Choice("?", {"flipper": None, "robot": None})}))


def test_choice_ohne_wahrscheinlichkeit_wird_unavailable(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {
            "q": {"type": "choice", "choice": "robot", "probabilities": {"flipper": 0.9}, "confidence": 0.9}},
            "usage": {}})
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Choice("?", {"flipper": None, "robot": None})}))


def test_nicht_serialisierbarer_state_ist_caller_bug(monkeypatch):
    _enabled(monkeypatch)
    def h(request):
        pytest.fail("Transport darf bei nicht-serialisierbarem state nie aufgerufen werden")
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(TypeError):
            asyncio.run(decide.decide({"x": object()}, {"q": Noul("?")}))
    assert decide.enabled() is True
