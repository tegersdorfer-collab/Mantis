"""Cloud-only Vision-Kette: keine Gesprächshistorie und kein lokaler Bild-Fallback."""

import asyncio
import base64
import json
import struct
from unittest.mock import AsyncMock

import httpx
import pytest

from core.backends.claude import ClaudeBackend, _ollama_msgs_to_anthropic
from core.backends.fallback import FallbackBackend
from core.backends.openrouter import OpenRouterBackend
from domains.inbox.vision import describe_images


def _png_bytes(width=1, height=1):
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def _image_block(data=b"png-data", mime="image/png"):
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def test_openrouter_luna_converts_vision_content_to_openai_image_url():
    backend = OpenRouterBackend(
        model="openai/gpt-6-luna",
        api_key="synthetic-key",
        client=httpx.AsyncClient(base_url="https://openrouter.invalid"),
    )
    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "Lies den Screenshot."}, _image_block()],
    }]

    payload = backend._payload(messages, None, 0.2, 120, False)

    assert backend.supports_vision is True
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "Lies den Screenshot."},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(b"png-data").decode("ascii")
        }},
    ]
    asyncio.run(backend._client.aclose())


def test_unsupported_openrouter_model_does_not_claim_vision():
    backend = OpenRouterBackend(
        model="qwen/qwen3.5-9b",
        api_key="synthetic-key",
        client=httpx.AsyncClient(base_url="https://openrouter.invalid"),
    )

    assert backend.supports_vision is False
    asyncio.run(backend._client.aclose())


def test_anthropic_haiku_converts_vision_content_to_base64_image_source():
    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    system, messages = _ollama_msgs_to_anthropic([{
        "role": "user",
        "content": [{"type": "text", "text": "Lies den Screenshot."}, _image_block()],
    }])

    assert backend.supports_vision is True
    assert system is None
    assert messages == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Lies den Screenshot."},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(b"png-data").decode("ascii"),
            }},
        ],
    }]


def test_describe_images_sends_only_caption_and_image_to_cloud_backend():
    backend = AsyncMock()
    backend.supports_vision = True
    backend.model_name = "openai/gpt-6-luna"
    backend.call.return_value = ("Screenshot mit Text: Wochenplan", [])

    image_bytes = _png_bytes()
    result = asyncio.run(describe_images(
        [{"data": image_bytes, "content_type": "image/jpeg"}],
        caption="Für später ansehen",
        backend=backend,
    ))

    assert result == "Screenshot mit Text: Wochenplan"
    kwargs = backend.call.await_args.kwargs
    assert len(kwargs["messages"]) == 1
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "Für später ansehen" in content[0]["text"]
    assert "OCR" in content[0]["text"]
    assert content[1]["source"]["data"] == base64.b64encode(image_bytes).decode("ascii")
    assert kwargs["tools"] is None
    assert kwargs["stream_cb"] is None
    assert content[1]["source"]["media_type"] == "image/png"


def test_describe_images_rejects_oversized_dimensions_before_provider_call():
    backend = AsyncMock()
    backend.supports_vision = True
    backend.model_name = "claude-haiku-5-5"

    result = asyncio.run(describe_images(
        [{"data": _png_bytes(8001, 1), "content_type": "image/png"}],
        caption="",
        backend=backend,
    ))

    assert result is None
    backend.call.assert_not_awaited()


def test_describe_images_keeps_total_json_payload_under_provider_limit():
    backend = AsyncMock()
    backend.supports_vision = True
    backend.model_name = "claude-haiku-5-5"
    backend.call.return_value = ("Vier Bilder geprüft", [])
    image = _png_bytes() + b"x" * (6_570_000 - len(_png_bytes()))

    result = asyncio.run(describe_images(
        [{"data": image, "content_type": "image/png"} for _ in range(4)],
        caption="", backend=backend,
    ))

    assert result == "Vier Bilder geprüft"
    messages = backend.call.await_args.kwargs["messages"]
    request_body = json.dumps({
        "messages": messages,
        "tools": None,
        "temperature": 0.1,
        "max_tokens": 500,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(request_body) < 32_000_000
    assert sum(block["type"] == "image" for block in messages[0]["content"]) == 3


def test_no_cloud_vision_stage_returns_none_without_trying_local_backend():
    local = AsyncMock()
    local.supports_vision = False
    local.model_name = "qwen3.5:9b"

    result = asyncio.run(describe_images(
        [{"data": b"image bytes", "content_type": "image/png"}],
        caption="",
        backend=local,
    ))

    assert result is None
    local.call.assert_not_awaited()


def test_cloud_failure_falls_to_another_vision_stage_but_never_to_local():
    first = AsyncMock()
    first.supports_vision = True
    first.model_name = "openai/gpt-6-luna"
    first.call.side_effect = RuntimeError("Cloud nicht erreichbar")
    second = AsyncMock()
    second.supports_vision = True
    second.model_name = "claude-haiku-5-5"
    second.call.return_value = ("Bild mit Speisekarte", [])
    local = AsyncMock()
    local.supports_vision = False
    local.model_name = "qwen3.5:9b"
    backend = FallbackBackend(
        backends=[first, second, local], stage_timeouts=[0.5, 0.5, None]
    )

    result = asyncio.run(describe_images(
        [{"data": _png_bytes(), "content_type": "image/webp"}],
        caption="Restaurant",
        backend=backend,
    ))

    assert result == "Bild mit Speisekarte"
    first.call.assert_awaited_once()
    second.call.assert_awaited_once()
    local.call.assert_not_awaited()


@pytest.mark.parametrize("content_type", ["image/svg+xml", "application/pdf", "text/plain"])
def test_unsupported_image_mime_is_not_sent_to_any_model(content_type):
    backend = AsyncMock()
    backend.supports_vision = True
    backend.model_name = "openai/gpt-6-luna"

    result = asyncio.run(describe_images(
        [{"data": b"not-a-supported-image", "content_type": content_type}],
        caption="",
        backend=backend,
    ))

    assert result is None
    backend.call.assert_not_awaited()


def test_image_with_base64_payload_over_claude_limit_is_not_sent_to_cloud():
    backend = AsyncMock()
    backend.supports_vision = True
    backend.model_name = "claude-haiku-5-5"

    result = asyncio.run(describe_images(
        [{"data": b"x" * 7_500_001, "content_type": "image/png"}],
        caption="",
        backend=backend,
    ))

    assert result is None
    backend.call.assert_not_awaited()
