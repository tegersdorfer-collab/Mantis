"""Offline-Tests der Mahlzeitenanalyse über die gemeinsame Cloud-Vision-Kette."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from core import vision
from web.routers import nutrition as nutrition_router


def test_nutrition_analysis_uses_cloud_vision_and_completes_meal(monkeypatch):
    calls = []
    completed = []
    failed = []

    async def ask_image(image_bytes, prompt, **kwargs):
        calls.append((image_bytes, prompt, kwargs))
        return '{"items":[{"name":"Brot","calories":200,"protein":8,"carbs":35,"fat":3}],"food_name":"Brot","portion":"1 Scheibe","confidence":0.8}'

    class OllamaForbidden:
        def __init__(self, **_kwargs):
            raise AssertionError("nutrition darf Ollama nicht aufrufen")

    monkeypatch.setattr(vision, "ask_image", ask_image)
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(AsyncClient=OllamaForbidden))
    monkeypatch.setattr(nutrition_router.nutrition, "complete_meal", lambda *args: completed.append(args))
    monkeypatch.setattr(nutrition_router.nutrition, "fail_meal", lambda *args: failed.append(args))

    asyncio.run(nutrition_router._run_analysis(5, b"food-photo", "mit Butter"))

    assert calls and calls[0][0] == b"food-photo"
    assert "mit Butter" in calls[0][1]
    assert calls[0][2] == {"max_tokens": 600, "temperature": 0.2, "format": "json"}
    assert completed == [(5, "mit Butter", 200.0, 8.0, 35.0, 3.0)]
    assert failed == []
