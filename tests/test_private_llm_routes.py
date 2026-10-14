"""Hintergrundrouten nutzen die Cloud-Background-Instanz statt den Chat-Agenten."""
import asyncio
import json
from types import SimpleNamespace

from starlette.requests import Request

from web.routers import fitness as fitness_router
from web.routers import journal as journal_router
from web.routers import tasks as tasks_router


def _endpoint(router, path, method=None):
    return next(
        route.endpoint for route in router.routes
        if route.path == path and (method is None or method in route.methods)
    )


class _BackgroundLLM:
    def __init__(self, response=""):
        self.response = response
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _ChatLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("Hintergrunddaten sollen nicht an das interaktive Chat-Backend gehen")


class _Extractor:
    def __init__(self):
        self.calls = []

    async def extract_from_exchange(self, user_text, assistant_text):
        self.calls.append((user_text, assistant_text))
        return 0


def _request(payload, path):
    body = json.dumps(payload).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 123),
        "server": ("test", 80),
    }, receive)


def test_manual_fitness_plan_uses_background_llm(monkeypatch):
    background = _BackgroundLLM()
    chat = _ChatLLM()
    received = []

    async def generate(chat_llm, bg_llm):
        received.append((chat_llm, bg_llm))
        return {"ok": True}

    monkeypatch.setattr(fitness_router.plan_generator, "generate_and_save", generate)
    endpoint = _endpoint(
        fitness_router.build_router(SimpleNamespace(chat_llm=chat, bg_llm=background)),
        "/api/fitness/plan/generate",
    )

    result = asyncio.run(endpoint(None))

    assert result == {"ok": True, "plan": {"ok": True}}
    assert received == [(None, background)]


def test_freeform_workout_import_uses_background_llm(monkeypatch):
    background = _BackgroundLLM(
        '{"title":"Krafttraining","type":"strength","exercises":[]}'
    )
    chat = _ChatLLM()
    monkeypatch.setattr(fitness_router.fitness, "log_workout", lambda **kwargs: 42)
    endpoint = _endpoint(
        fitness_router.build_router(SimpleNamespace(chat_llm=chat, bg_llm=background)),
        "/api/fitness/import",
    )

    class Request:
        async def json(self):
            return {"text": "Kniebeugen und Bankdrücken, jeweils 3 Sätze."}

    result = asyncio.run(endpoint(Request()))

    assert result["id"] == 42
    assert background.calls and "Kniebeugen" in background.calls[0]["messages"][0]["content"]


def test_journal_prompts_use_background_llm_for_recent_entries_and_mood(monkeypatch):
    background = _BackgroundLLM("Frage eins\nFrage zwei\nFrage drei")
    chat = _ChatLLM()
    monkeypatch.setattr(journal_router.journal, "recent_entries", lambda _: [{"content": "Stress nach dem Lauf"}])
    monkeypatch.setattr(journal_router.journal, "mood_trend", lambda _: [{"mood": 2}])
    endpoint = _endpoint(
        journal_router.build_router(SimpleNamespace(chat_llm=chat, bg_llm=background)),
        "/api/journal/prompts",
    )

    result = asyncio.run(endpoint())

    assert result["prompts"] == ["Frage eins", "Frage zwei", "Frage drei"]
    prompt = background.calls[0]["messages"][0]["content"]
    assert "Stress nach dem Lauf" in prompt
    assert "2.0/5" in prompt


def test_journal_extraction_uses_cloud_extractor_with_chat_cloud_configured(monkeypatch):
    extractor = _Extractor()
    monkeypatch.setattr(journal_router.journal, "add_entry", lambda **_: 17)
    endpoint = _endpoint(journal_router.build_router(SimpleNamespace(
        extractor=extractor,
    )), "/api/journal", "POST")

    async def run():
        result = await endpoint(_request({
            "content": "Fiktiver privater Eintrag",
            "prompts_answers": [{"question": "Beispiel?", "answer": "Fiktive Antwort"}],
        }, "/api/journal"))
        await asyncio.sleep(0)
        return result

    assert asyncio.run(run()) == {"id": 17}
    assert len(extractor.calls) == 1
    assert "Fiktiver privater Eintrag" in extractor.calls[0][0]
    assert "Fiktive Antwort" in extractor.calls[0][0]


def test_task_classification_uses_background_llm_for_title_and_notes(monkeypatch):
    chat = _ChatLLM()
    background = _BackgroundLLM("MANTIS")
    monkeypatch.setattr(tasks_router.tasks_d, "create_task", lambda **_: 23)
    monkeypatch.setattr(tasks_router.db, "execute", lambda *args, **kwargs: 1)
    endpoint = _endpoint(
        tasks_router.build_router(SimpleNamespace(chat_llm=chat, bg_llm=background)),
        "/api/tasks",
        "POST",
    )

    result = asyncio.run(endpoint(_request({
        "title": "Private Aufgabe", "notes": "Vertrauliche Notiz",
    }, "/api/tasks")))

    assert result == {"id": 23}
    assert chat.calls == []
    assert len(background.calls) == 1
    prompt = background.calls[0]["messages"][0].content
    assert "Private Aufgabe" in prompt
    assert "Vertrauliche Notiz" in prompt


def test_task_suggestions_use_background_llm_for_lzg_context(monkeypatch):
    chat = _ChatLLM()
    background = _BackgroundLLM("background suggestion")
    lzg = object()

    async def suggest_one(trigger, llm, memories):
        await llm.chat(messages=[{"role": "user", "content": "suggest from saved context"}])
        return True

    monkeypatch.setattr(tasks_router, "suggest_one", suggest_one)
    endpoint = _endpoint(
        tasks_router.build_router(SimpleNamespace(chat_llm=chat, bg_llm=background, lzg=lzg)),
        "/api/tasks/generate",
    )

    async def run():
        result = await endpoint()
        await asyncio.sleep(0)
        return result

    assert asyncio.run(run()) == {"ok": True}
    assert chat.calls == []
    assert len(background.calls) == 1
