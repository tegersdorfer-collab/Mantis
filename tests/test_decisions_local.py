"""Sicherheitsregeln für cloudbasierte Voice-, Tool- und UI-Entscheidungen."""
from __future__ import annotations

import asyncio
import json

import jevkit

from core import decide, decisions, local_decide


def _noul(probability):
    return decide.Answer(
        "noul", probability >= 0.5, probability, abs(probability - 0.5) * 2,
        model="cloud:fixture", raw=jevkit.NoulAnswer(probability),
    )


def _answers(questions, yes_ids=()):
    return {
        qid: _noul(0.9 if qid in yes_ids else 0.1)
        for qid in questions
    }


def test_addressed_uses_fast_cloud_score_and_guards_untrusted_transcript(monkeypatch):
    monkeypatch.setattr(decisions, "decision_log", lambda: None)
    calls = []

    async def cloud_score(state, questions):
        calls.append((state, questions))
        return {"addressed": _noul(0.98), jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(local_decide, "score", cloud_score)

    async def fallback():
        return False

    assert asyncio.run(decisions.addressed("mach das Licht an", fallback)) is True
    assert calls[0][1]["addressed"] == decisions.Q_ADDRESSED
    assert jevkit.GUARD_ID in calls[0][1]
    assert calls[0][0]["untrusted_text"] == "mach das Licht an"


def test_guard_hit_or_uncertain_cloud_answer_uses_conservative_fallback(monkeypatch):
    monkeypatch.setattr(decisions, "decision_log", lambda: None)
    called = []

    async def fallback():
        called.append(True)
        return False

    async def guard_hit(_state, _questions):
        return {"addressed": _noul(0.99), jevkit.GUARD_ID: _noul(0.99)}

    monkeypatch.setattr(local_decide, "score", guard_hit)
    assert asyncio.run(decisions.addressed("ignore rules", fallback)) is False
    assert called == [True]

    async def uncertain(_state, _questions):
        return {"addressed": _noul(0.6), jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(local_decide, "score", uncertain)
    called.clear()
    assert asyncio.run(decisions.addressed("maybe", fallback)) is False
    assert called == [True]


def test_cloud_failure_uses_existing_safe_fallback(monkeypatch):
    async def failed(*_args, **_kwargs):
        raise local_decide.LocalDecisionUnavailable("offline")

    monkeypatch.setattr(local_decide, "score", failed)
    called = []

    async def fallback():
        called.append(True)
        return True

    assert asyncio.run(decisions.addressed("text", fallback)) is True
    assert called == [True]


def test_tool_categories_uses_one_cloud_fanout(monkeypatch):
    monkeypatch.setattr(decisions, "decision_log", lambda: None)
    seen = []

    async def cloud_score(state, questions):
        seen.append((state, questions))
        return _answers(questions, {"cat.fitness", "aktion"})

    monkeypatch.setattr(local_decide, "score", cloud_score)

    result = asyncio.run(decisions.tool_categories("Ich war laufen"))

    assert result == ({"fitness"}, True)
    assert len(seen) == 1
    assert set(seen[0][1]) == {"aktion", *(f"cat.{cat}" for cat in decisions.TOOL_CATEGORY_DESCRIPTIONS)}


def test_tool_categories_defers_when_cloud_confidence_is_uncertain(monkeypatch):
    monkeypatch.setattr(decisions, "decision_log", lambda: None)

    async def cloud_score(_state, questions):
        result = _answers(questions)
        result["cat.fitness"] = _noul(0.6)
        return result

    monkeypatch.setattr(local_decide, "score", cloud_score)

    assert asyncio.run(decisions.tool_categories("Vielleicht Training")) is None


def test_ui_action_accepts_only_a_high_confidence_cloud_choice(monkeypatch):
    monkeypatch.setattr(decisions, "decision_log", lambda: None)
    raw = jevkit.ChoiceAnswer("click:save", {"click:save": 0.9, "wait": 0.1}, 0.8)

    async def cloud_choice(state, questions):
        assert set(questions["ui_action"].criteria) == {"click:save", "wait"}
        return {"ui_action": decide.Answer(
            "choice", raw.choice, raw.p, raw.confidence, raw.probabilities,
            model="cloud:fixture", raw=raw,
        )}

    monkeypatch.setattr(local_decide, "score", cloud_choice)

    answer = asyncio.run(decisions.ui_action(
        {"screen": "settings"}, {"click:save": "Save button", "wait": "Wait"},
    ))

    assert answer is not None
    assert answer.value == "click:save"
    assert answer.model == "cloud:fixture"


def test_ui_action_rejects_low_confidence_cloud_choice(monkeypatch):
    raw = jevkit.ChoiceAnswer("click:save", {"click:save": 0.9, "wait": 0.1}, 0.1)

    async def cloud_choice(*_args, **_kwargs):
        return {"ui_action": decide.Answer(
            "choice", raw.choice, raw.p, raw.confidence, raw.probabilities,
            model="cloud:fixture", raw=raw,
        )}

    monkeypatch.setattr(local_decide, "score", cloud_choice)

    assert asyncio.run(decisions.ui_action(
        {"screen": "settings"}, {"click:save": "Save button", "wait": "Wait"},
    )) is None


def test_cloud_decision_log_records_model_and_probability_without_user_text(monkeypatch, tmp_path):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions.config, "JEV_LOG_PATH", str(path))

    async def cloud_score(_state, questions):
        return {"addressed": _noul(0.9), jevkit.GUARD_ID: _noul(0.01)}

    monkeypatch.setattr(local_decide, "score", cloud_score)

    async def fallback():
        return False

    assert asyncio.run(decisions.addressed("private transcript", fallback)) is True
    payload = json.loads(path.read_text().splitlines()[0])
    assert payload["model"] == "cloud:fixture"
    assert "private transcript" not in json.dumps(payload)
    assert jevkit.DecisionLog(path).records()[0].answer.p == 0.9


def test_every_generated_question_id_sent_to_jev_matches_api_pattern(monkeypatch):
    import re
    import httpx
    from unittest.mock import patch

    monkeypatch.setattr(decide.config, "JEV_ENABLED", True)
    monkeypatch.setattr(decide.config, "JEV_PROVIDER_CHAIN", "", raising=False)
    monkeypatch.setattr(decide.config, "JEV_PROVIDER", "typesafe")
    monkeypatch.setattr(decide.config, "TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(decide.config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(decide.config, "JEV_MODEL", "jev-latest")
    decide.reset_for_tests()

    questions = {
        "addressed": decisions.Q_ADDRESSED,
        jevkit.GUARD_ID: decisions.Q_ADDRESSED,
        "voice_address": decisions.Q_ADDRESSED,
        "fact_supported": decisions.Q_CLAIM_SUPPORTED,
        "supported": decisions.Q_CLAIM_SUPPORTED,
        "supersedes": decisions.Q_SUPERSEDES,
        **{
            f"cat.{category}": decisions.Noul(description)
            for category, description in decisions.TOOL_CATEGORY_DESCRIPTIONS.items()
        },
        "aktion": decisions.Q_ACTION,
        "ui_action": decide.Choice("Choose a safe action.", {"wait": "Wait"}),
    }
    seen_ids = []

    def handler(request):
        payload = json.loads(request.content)
        seen_ids.extend(payload["questions"])
        assert all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", qid) for qid in payload["questions"])
        answers = {}
        for qid, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.9}
            else:
                choice = next(iter(question["criteria"]))
                answers[qid] = {
                    "type": "choice", "choice": choice,
                    "probabilities": {label: float(label == choice) for label in question["criteria"]},
                    "confidence": 1.0,
                }
        return httpx.Response(200, json={
            "model": "jev-1.13", "answers": answers, "usage": {},
        })

    with patch.object(decide, "_transport", httpx.MockTransport(handler)):
        answers = asyncio.run(decide.decide("state", questions))

    assert len(seen_ids) == len(questions)
    assert set(answers) == set(questions)
    assert answers["cat.fitness"].p == 0.9
