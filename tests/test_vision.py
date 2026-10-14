"""Offline-Tests für die Cloud-Bildanalyse."""
from __future__ import annotations

import asyncio
import base64

from core import vision
from core.backends.base import AgentBackend


class FakeVisionBackend(AgentBackend):
    def __init__(self, answer="Ein Bild"):
        self.answer = answer
        self.calls = []

    @property
    def supports_vision(self):
        return True

    @property
    def model_name(self):
        return "openai/gpt-6-luna"

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls.append({"messages": messages, "tools": tools, "temperature": temperature,
                           "max_tokens": max_tokens})
        return self.answer, []

    async def warmup(self):
        return None


def test_ask_image_routes_cloud_vision_and_encodes_image(monkeypatch):
    backend = FakeVisionBackend()
    purposes = []

    def cloud_chain(purpose, **kwargs):
        purposes.append((purpose, kwargs))
        return backend

    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", cloud_chain)

    result = asyncio.run(vision.ask_image(b"image-bytes", "Beschreibe es.", max_tokens=77))

    assert result == "Ein Bild"
    assert purposes == [("vision", {"reasoning": "low"})]
    assert backend.calls[0]["max_tokens"] == 77
    content = backend.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Beschreibe es."}
    assert content[1] == {
        "type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(b"image-bytes").decode(),
        },
    }


def test_ask_image_requests_json_when_format_is_json(monkeypatch):
    backend = FakeVisionBackend('{"items":[]}')
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", lambda *_args, **_kwargs: backend)

    result = asyncio.run(vision.ask_image(b"image", "Schätze Makros.", format="json"))

    assert result == '{"items":[]}'
    assert "validem JSON" in backend.calls[0]["messages"][0]["content"]


def test_describe_image_returns_fallback_when_cloud_fails(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("Cloud nicht verfügbar")

    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", fail)

    assert "nicht analysieren" in asyncio.run(
        vision.describe_image(b"image", "Beschreibe es.")
    ).lower()
