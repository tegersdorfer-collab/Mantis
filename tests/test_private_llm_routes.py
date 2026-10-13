"""Gesundheitsnahe Daten aus Fitness- und Journal-Endpunkten bleiben lokal."""
import asyncio
from types import SimpleNamespace

from web.routers import fitness as fitness_router
from web.routers import journal as journal_router


def _endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


class _LocalLLM:
    def __init__(self, response=""):
        self.response = response
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _CloudLLM:
    async def chat(self, **kwargs):
        raise AssertionError("Gesundheitsdaten dürfen nicht an chat_llm gehen")


def test_manual_fitness_plan_uses_local_background_llm(monkeypatch):
    local = _LocalLLM()
    cloud = _CloudLLM()
    received = []

    async def generate(chat_llm, bg_llm):
        received.append((chat_llm, bg_llm))
        return {"ok": True}

    monkeypatch.setattr(fitness_router.plan_generator, "generate_and_save", generate)
    endpoint = _endpoint(
        fitness_router.build_router(SimpleNamespace(chat_llm=cloud, bg_llm=local)),
        "/api/fitness/plan/generate",
    )

    result = asyncio.run(endpoint(None))

    assert result == {"ok": True, "plan": {"ok": True}}
    assert received == [(None, local)]


def test_freeform_workout_import_uses_local_background_llm(monkeypatch):
    local = _LocalLLM(
        '{"title":"Krafttraining","type":"strength","exercises":[]}'
    )
    cloud = _CloudLLM()
    monkeypatch.setattr(fitness_router.fitness, "log_workout", lambda **kwargs: 42)
    endpoint = _endpoint(
        fitness_router.build_router(SimpleNamespace(chat_llm=cloud, bg_llm=local)),
        "/api/fitness/import",
    )

    class Request:
        async def json(self):
            return {"text": "Kniebeugen und Bankdrücken, jeweils 3 Sätze."}

    result = asyncio.run(endpoint(Request()))

    assert result["id"] == 42
    assert local.calls and "Kniebeugen" in local.calls[0]["messages"][0]["content"]


def test_journal_prompts_use_local_llm_for_recent_entries_and_mood(monkeypatch):
    local = _LocalLLM("Frage eins\nFrage zwei\nFrage drei")
    cloud = _CloudLLM()
    monkeypatch.setattr(journal_router.journal, "recent_entries", lambda _: [{"content": "Stress nach dem Lauf"}])
    monkeypatch.setattr(journal_router.journal, "mood_trend", lambda _: [{"mood": 2}])
    endpoint = _endpoint(
        journal_router.build_router(SimpleNamespace(chat_llm=cloud, bg_llm=local)),
        "/api/journal/prompts",
    )

    result = asyncio.run(endpoint())

    assert result["prompts"] == ["Frage eins", "Frage zwei", "Frage drei"]
    prompt = local.calls[0]["messages"][0]["content"]
    assert "Stress nach dem Lauf" in prompt
    assert "2.0/5" in prompt
