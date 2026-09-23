"""Fallback-Verhalten mit optionalem lokalem Logit-Scoring."""
from __future__ import annotations

import asyncio
import json

import jevkit

from core import decide, decisions, local_decide


def _noul(p, metadata=None):
    return decide.Answer("noul", p >= 0.5, p, abs(p - 0.5) * 2,
                         model="local-logits:qwen3.5:9b", raw=jevkit.NoulAnswer(p),
                         metadata=metadata or {})


def test_flag_aus_laesst_bisherigen_jev_ausfallpfad_unveraendert(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", False)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    async def local_forbidden(*args, **kwargs):
        raise AssertionError("lokales Scoring ist per Default aus")

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_forbidden)
    called = []

    async def fallback():
        called.append(True)
        return False

    assert asyncio.run(decisions.addressed("Text", fallback)) is False
    assert called == [True]


def test_flag_an_nimmt_lokales_ergebnis_nur_bei_act(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    calls = []

    async def local_answer(state, questions, **kwargs):
        calls.append((state, questions))
        return {"addressed": _noul(0.9), jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_answer)
    fallback_calls = []

    async def fallback():
        fallback_calls.append(True)
        return False

    assert asyncio.run(decisions.addressed("Text", fallback)) is True
    assert len(calls) == 1
    assert calls[0][1]["addressed"] == decisions.Q_ADDRESSED
    assert jevkit.GUARD_ID in calls[0][1]
    assert fallback_calls == []


def test_flag_an_nimmt_unsichere_oder_fehlgeschlagene_lokale_antwort_nicht(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    state = {"answer": _noul(0.6)}

    async def local_answer(*args, **kwargs):
        return {"addressed": state["answer"], jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(local_decide, "score", local_answer)
    called = []

    async def fallback():
        called.append(True)
        return False

    assert asyncio.run(decisions.addressed("Text", fallback)) is False
    assert called == [True]

    async def local_error(*args, **kwargs):
        raise local_decide.LocalDecisionUnavailable("offline")

    monkeypatch.setattr(local_decide, "score", local_error)
    called.clear()
    assert asyncio.run(decisions.addressed("Text", fallback)) is False
    assert called == [True]


def test_lokaler_guard_treffer_bleibt_im_fallback(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    async def local_answers(*args, **kwargs):
        return {"addressed": _noul(0.99), jevkit.GUARD_ID: _noul(0.99)}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_answers)
    called = []

    async def fallback():
        called.append(True)
        return False

    assert asyncio.run(decisions.addressed("Manipulation", fallback)) is False
    assert called == [True]


def test_lokale_antwort_wird_mit_modellname_ins_jev_log_geschrieben(monkeypatch, tmp_path):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions.config, "JEV_LOG_PATH", str(tmp_path / "decisions.jsonl"))

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    async def local_answer(*args, **kwargs):
        return {"addressed": _noul(0.9, {"missing_labels": ["B"]}), jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_answer)

    async def fallback():
        return False

    assert asyncio.run(decisions.addressed("not-a-secret", fallback)) is True
    payload = json.loads((tmp_path / "decisions.jsonl").read_text().splitlines()[0])
    assert payload["model"] == "local-logits:qwen3.5:9b"
    assert payload["answer"]["metadata"]["missing_labels"] == ["B"]
    assert "not-a-secret" not in json.dumps(payload)
    record = jevkit.DecisionLog(tmp_path / "decisions.jsonl").records()[0]
    assert record.answer.p == 0.9


def test_tool_categories_nutzt_nach_jev_ausfall_nur_sichere_lokale_antworten(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    seen = []

    async def local_answers(state, questions, **kwargs):
        seen.append(questions)
        return {qid: _noul(0.9 if qid in {"cat:fitness", "aktion"} else 0.1)
                for qid in questions}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_answers)

    result = asyncio.run(decisions.tool_categories("Ich war laufen"))

    assert result == ({"fitness"}, True)
    assert set(seen[0]) == {"aktion", *(f"cat:{cat}" for cat in decisions.TOOL_CATEGORY_DESCRIPTIONS)}


def test_tool_categories_faellt_bei_lokalem_confirm_zurueck(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    async def local_answers(state, questions, **kwargs):
        return {qid: _noul(0.6 if qid == "cat:fitness" else 0.1) for qid in questions}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_answers)

    assert asyncio.run(decisions.tool_categories("Vielleicht Training")) is None


def test_ui_action_nutzt_lokales_choice_nur_bei_act(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    raw = jevkit.ChoiceAnswer("click:save", {"click:save": 0.9, "wait": 0.1}, 0.8)

    async def local_choice(state, questions, **kwargs):
        return {"ui_action": decide.Answer(
            "choice", raw.value, raw.p, raw.confidence, raw.probabilities,
            model="local-logits:qwen3.5:9b", raw=raw,
        )}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_choice)

    answer = asyncio.run(decisions.ui_action(
        {"screen": "settings"}, {"click:save": "Save button", "wait": "Wait"},
    ))

    assert answer is not None
    assert answer.value == "click:save"
    assert answer.model == "local-logits:qwen3.5:9b"


def test_ui_action_lehnt_unsichere_lokale_choice_ab(monkeypatch):
    monkeypatch.setattr(decisions.config, "LOCAL_LOGITS_ENABLED", True)
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def jev_offline(*args, **kwargs):
        raise decide.JevUnavailable("offline")

    raw = jevkit.ChoiceAnswer("click:save", {"click:save": 0.9, "wait": 0.1}, 0.1)

    async def local_choice(*args, **kwargs):
        return {"ui_action": decide.Answer(
            "choice", raw.value, raw.p, raw.confidence, raw.probabilities,
            model="local-logits:qwen3.5:9b", raw=raw,
        )}

    monkeypatch.setattr(decisions.decide, "decide", jev_offline)
    monkeypatch.setattr(local_decide, "score", local_choice)

    assert asyncio.run(decisions.ui_action(
        {"screen": "settings"}, {"click:save": "Save button", "wait": "Wait"},
    )) is None
