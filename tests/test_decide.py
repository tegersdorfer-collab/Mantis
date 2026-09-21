"""core/decide.py: Jev-HTTP-Engine mit Circuit-Breaker. Kein Netz, alles per MockTransport."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
from unittest.mock import patch

import httpx
import jevkit
import pytest

from core import decide
from core.decide import Noul, Choice, JevUnavailable


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
    monkeypatch.setattr(decide.config, "JEV_PROVIDER", "openrouter")
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(decide.config, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(decide.config, "JEV_MODEL", "~typesafe/jev-latest")
    decide.reset_for_tests()


def _enabled_typesafe(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "JEV_PROVIDER", "typesafe")
    monkeypatch.setattr(decide.config, "TYPESAFE_API_KEY", "sk-test")
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(decide.config, "JEV_MODEL", "jev-latest")
    decide.reset_for_tests()


def test_disabled_ohne_key(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "JEV_PROVIDER", "typesafe")
    monkeypatch.setattr(decide.config, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "")
    decide.reset_for_tests()
    assert decide.enabled() is False
    with pytest.raises(JevUnavailable):
        asyncio.run(decide.decide("x", {"q": Noul("Ist x?")}))


def test_direct_typesafe_backend_sendet_direct_endpoint(monkeypatch):
    _enabled_typesafe(monkeypatch)

    def h(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert body["model"] == "jev-latest"
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {"q": {"type": "noul", "noul": 0.93}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    with patch.object(decide, "_transport", _transport(h)):
        out = asyncio.run(decide.decide("x", {"q": Noul("Ist x?")}))

    assert out["q"].model == "jev-1.13.0"
    assert out["q"].p == 0.93


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


def test_from_jevkit_keeps_score_probabilities():
    score = jevkit.ScoreAnswer(2.0, {"0": "low", "1": "mid", "2": "high"},
                               {"0": 0.1, "1": 0.2, "2": 0.7}, 0.6)

    answer = decide.Answer.from_jevkit(score, model="jev-1.13")

    assert answer.kind == "score"
    assert answer.probabilities == {"0": 0.1, "1": 0.2, "2": 0.7}


def test_from_jevkit_rejects_unknown_answer_type():
    class FutureAnswer:
        kind = "future"
        value = "x"
        p = 1.0
        confidence = 1.0

    with pytest.raises(TypeError, match="unsupported Jev answer type"):
        decide.Answer.from_jevkit(FutureAnswer())


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


def test_gesamtbudget_timeout_wird_unavailable(monkeypatch):
    """asyncio.timeout ist das GESAMTBUDGET über Connect+Read+Parse — nicht nur die
    httpx-Phasen-Timeout — und muss wie jeder andere Fehler den Breaker öffnen."""
    _enabled(monkeypatch)
    monkeypatch.setattr(decide.config, "JEV_TIMEOUT_S", 0.05)
    async def h(request):
        await asyncio.sleep(0.2)
        return _ok_response(request)
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert decide.enabled() is False


def test_unbekannter_antworttyp_oeffnet_breaker(monkeypatch):
    """Ein geändertes Antwortschema soll nicht jeden Turn erneut den vollen
    Roundtrip kosten, bevor lokal zurückgefallen wird — also Breaker auf."""
    _enabled(monkeypatch)
    def h(request):
        return httpx.Response(200, json={"model": "m", "answers": {
            "q": {"type": "score", "score": 0.5}}, "usage": {}})
    with patch.object(decide, "_transport", _transport(h)):
        with pytest.raises(JevUnavailable):
            asyncio.run(decide.decide("x", {"q": Noul("?")}))
    assert decide.enabled() is False


def test_breaker_ueberlebt_parallelen_erfolg(monkeypatch):
    """Jeder decide()-Call baut seinen eigenen Client mit dem Breaker-Stand von seinem
    Start — ein langsamer, aber erfolgreicher Call darf beim Zurückschreiben den Breaker
    nicht wieder schließen, den ein inzwischen fertiger, fehlgeschlagener Call geöffnet
    hat (Lost Update ohne max()-Spiegelung)."""
    _enabled(monkeypatch)
    monkeypatch.setattr(decide.config, "JEV_COOLDOWN_S", 1000.0)

    async def h(request):
        body = json.loads(request.content)
        if body["state"] == "fail":
            # Kein sleep: dieser Call ist immer fertig, lange bevor "ok" aufwacht.
            return httpx.Response(503)
        await asyncio.sleep(0.05)
        return _ok_response(request)

    async def run():
        return await asyncio.gather(
            decide.decide("ok", {"q": Noul("?")}),
            decide.decide("fail", {"q": Noul("?")}),
            return_exceptions=True,
        )

    with patch.object(decide, "_transport", _transport(h)):
        results = asyncio.run(run())

    ok_result, fail_result = results
    assert isinstance(fail_result, JevUnavailable)
    assert not isinstance(ok_result, Exception)
    # Der Breaker muss offen bleiben, auch nachdem der langsame Erfolg zurückgeschrieben hat.
    assert decide.enabled() is False
