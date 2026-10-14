"""Offline-Tests für die Cloud-Bildanalyse."""
from __future__ import annotations

import asyncio
import base64
import struct

import pytest

from core import vision
from core.backends.base import AgentBackend


def _png(width=1, height=1):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


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

    png_bytes = _png()
    result = asyncio.run(vision.ask_image(png_bytes, "Beschreibe es.", max_tokens=77))

    assert result == "Ein Bild"
    assert purposes == [("vision", {"reasoning": "low"})]
    assert backend.calls[0]["max_tokens"] == 77
    content = backend.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Beschreibe es."}
    assert content[1] == {
        "type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode(),
        },
    }


def test_ask_image_requests_json_when_format_is_json(monkeypatch):
    backend = FakeVisionBackend('{"items":[]}')
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", lambda *_args, **_kwargs: backend)

    result = asyncio.run(vision.ask_image(_png(), "Schätze Makros.", format="json"))

    assert result == '{"items":[]}'
    assert "validem JSON" in backend.calls[0]["messages"][0]["content"]


def test_ask_image_rejects_base64_payload_over_anthropic_limit_before_chain(monkeypatch):
    called = []
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", lambda *_args, **_kwargs: called.append(True))

    with pytest.raises(ValueError, match="10 MB"):
        asyncio.run(vision.ask_image(b"x" * 7_500_001, "Beschreibe es."))

    assert called == []


def test_ask_image_rejects_dimensions_over_provider_limit_before_chain(monkeypatch):
    called = []
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", lambda *_args, **_kwargs: called.append(True))

    with pytest.raises(ValueError, match="8000x8000"):
        asyncio.run(vision.ask_image(_png(8001, 1), "Beschreibe es."))

    assert called == []


def test_ask_image_rejects_unrecognized_image_bytes_before_chain(monkeypatch):
    called = []
    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", lambda *_args, **_kwargs: called.append(True))

    with pytest.raises(ValueError, match="Bildformat"):
        asyncio.run(vision.ask_image(b"image-bytes", "Beschreibe es."))

    assert called == []


def test_describe_image_returns_fallback_when_cloud_fails(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("Cloud nicht verfügbar")

    monkeypatch.setattr(vision, "_chain", None)
    monkeypatch.setattr(vision, "cloud_chain", fail)

    assert "nicht analysieren" in asyncio.run(
        vision.describe_image(b"image", "Beschreibe es.")
    ).lower()
