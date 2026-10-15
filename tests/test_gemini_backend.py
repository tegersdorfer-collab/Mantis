"""Vertex-Gemini-Backend: HTTP-Verhalten wird ausschließlich gemockt."""
import asyncio
import json
import time

import httpx
import pytest

from core.backends.fallback import FallbackBackend
from core.backends.gemini import GeminiBackend
from core.agent import Agent


@pytest.fixture(autouse=True)
def _avoid_usage_database_writes(monkeypatch):
    from core import llm_usage

    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: None)


def _backend(handler, *, purpose="chat-agent"):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://aiplatform.googleapis.com",
    )
    return GeminiBackend(
        model="gemini-3-flash-test",
        api_key="gemini-test-key",
        client=client,
        purpose=purpose,
    )


def _response(*, text="", function_call=None, usage=None):
    parts = []
    if text:
        parts.append({"text": text})
    if function_call is not None:
        parts.append({"functionCall": function_call})
    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": "STOP",
        }],
        "usageMetadata": usage or {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }


def _call(backend, **kwargs):
    async def run():
        try:
            return await backend.call(
                messages=kwargs.pop("messages"),
                tools=kwargs.pop("tools", None),
                stream_cb=kwargs.pop("stream_cb", None),
                temperature=kwargs.pop("temperature", 0.4),
                max_tokens=kwargs.pop("max_tokens", 120),
                **kwargs,
            )
        finally:
            await backend._client.aclose()

    return asyncio.run(run())


def test_vertex_request_maps_system_history_tools_and_inline_image(monkeypatch):
    from core import llm_usage

    seen = {}
    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    monkeypatch.setattr("config.GEMINI_USD_PER_MTOK_IN", 0.5, raising=False)
    monkeypatch.setattr("config.GEMINI_USD_PER_MTOK_OUT", 2.0, raising=False)

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response(text="Gefunden."))

    backend = _backend(handler)
    text, calls = _call(
        backend,
        messages=[
            {"role": "system", "content": "Du bist Mantis."},
            {"role": "user", "content": [
                {"type": "text", "text": "Was ist auf dem Bild?"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": "aGVsbG8=",
                }},
            ]},
            {"role": "assistant", "content": "Ich suche."},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1",
                "function": {"name": "lookup", "arguments": {"query": "Mantis"}},
            }]},
            {"role": "tool", "name": "lookup", "tool_use_id": "call-1", "content": "Treffer"},
            {"role": "user", "content": "Fasse zusammen."},
        ],
        tools=[{"type": "function", "function": {
            "name": "lookup",
            "description": "Sucht einen Eintrag.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
    )

    assert text == "Gefunden."
    assert calls == []
    assert seen["url"] == (
        "https://aiplatform.googleapis.com/v1/publishers/google/models/"
        "gemini-3-flash-test:generateContent"
    )
    assert seen["key"] == "gemini-test-key"
    body = seen["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "Du bist Mantis."}]}
    assert body["contents"] == [
        {"role": "user", "parts": [
            {"text": "Was ist auf dem Bild?"},
            {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}},
        ]},
        {"role": "model", "parts": [{"text": "Ich suche."}, {
            "functionCall": {"id": "call-1", "name": "lookup", "args": {"query": "Mantis"}},
        }]},
        {"role": "user", "parts": [
            {"functionResponse": {
                "id": "call-1", "name": "lookup", "response": {"result": "Treffer"},
            }},
            {"text": "Fasse zusammen."},
        ]},
    ]
    assert body["tools"] == [{"functionDeclarations": [{
        "name": "lookup",
        "description": "Sucht einen Eintrag.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }]}]
    assert body["generationConfig"] == {"temperature": 0.4, "maxOutputTokens": 120}
    assert records == [(("gemini", "gemini-3-flash-test", 10, 5), {
        "purpose": "chat-agent", "actual_cost": pytest.approx(0.000015),
    })]


def test_forced_tool_call_uses_vertex_any_mode_and_returns_normalized_call():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response(function_call={
            "id": "google-call-7", "name": "lookup", "args": {"query": "Nürnberg"},
        }))

    backend = _backend(handler)
    text, calls = _call(
        backend,
        messages=[{"role": "user", "content": "Suche Nürnberg."}],
        tools=[{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
        force_tool_call=True,
    )

    assert text == ""
    assert calls == [{
        "id": "google-call-7",
        "tool_use_id": "google-call-7",
        "function": {"name": "lookup", "arguments": {"query": "Nürnberg"}},
    }]
    assert seen["body"]["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY"},
    }


def test_stream_uses_sse_and_fallback_emits_attempt_events():
    chunks = [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hallo"}]}}]},
        {"candidates": [{
            "content": {"role": "model", "parts": [{"text": " Welt"}]},
            "finishReason": "STOP",
        }], "usageMetadata": {
            "promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6,
        }},
    ]
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    backend = _backend(handler)
    chain = FallbackBackend(backends=[backend], stage_timeouts=[2], cooldown_s=0)
    events = []

    async def collect(event):
        events.append(event)

    async def exercise():
        try:
            return await Agent(chain).run(
                messages=[{"role": "user", "content": "Sag Hallo."}],
                system="Du antwortest knapp.",
                stream_cb=collect,
                use_tools=False,
                max_tokens=40,
            )
        finally:
            await backend._client.aclose()

    text, trace = asyncio.run(exercise())

    assert (text, trace) == ("Hallo Welt", [])
    assert seen["url"].endswith(":streamGenerateContent?alt=sse")
    assert seen["body"]["generationConfig"]["maxOutputTokens"] == 40
    assert [event.kind for event in events] == [
        "attempt_start", "delta", "delta", "attempt_end",
    ]
    assert [event.text_delta for event in events if event.kind == "delta"] == ["Hallo", " Welt"]
    assert len({event.attempt_id for event in events}) == 1
    assert events[-1].final is True


def test_streamed_function_call_is_returned_in_agent_tool_format():
    event = {"candidates": [{
        "content": {"role": "model", "parts": [{"functionCall": {
            "id": "stream-call-1", "name": "lookup", "args": {"query": "Nürnberg"},
        }}]},
        "finishReason": "STOP",
    }]}

    def handler(_request):
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    backend = _backend(handler)
    chunks = []

    async def collect(chunk):
        chunks.append(chunk)

    text, calls = _call(
        backend,
        messages=[{"role": "user", "content": "Suche Nürnberg."}],
        tools=[{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
        stream_cb=collect,
    )

    assert text == ""
    assert chunks == []
    assert calls == [{
        "id": "stream-call-1",
        "tool_use_id": "stream-call-1",
        "function": {"name": "lookup", "arguments": {"query": "Nürnberg"}},
    }]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(("status", "error", "expected_requests"), [
    (400, {"status": "INVALID_ARGUMENT", "message": "bad request"}, 2),
    (429, {"status": "RESOURCE_EXHAUSTED", "message": "quota"}, 1),
    (400, {"status": "RESOURCE_EXHAUSTED", "message": "quota exceeded"}, 1),
    (403, {"status": "PERMISSION_DENIED", "message": "Quota limit"}, 1),
])
def test_quota_errors_cool_down_but_bad_request_does_not(
    status, error, expected_requests, streaming,
):
    requests = []

    def handler(_request):
        requests.append(True)
        return httpx.Response(status, json={"error": {"code": status, **error}})

    backend = _backend(handler)

    class Healthy:
        model_name = "fallback-model"

        async def call(self, **_kwargs):
            return "Fallback-Antwort", []

    chain = FallbackBackend(backends=[backend, Healthy()], stage_timeouts=[2, 2], cooldown_s=60)

    async def exercise():
        try:
            async def discard_chunk(_chunk):
                return None

            for _ in range(2):
                await chain.call(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    stream_cb=discard_chunk if streaming else None,
                    temperature=0.2,
                    max_tokens=20,
                )
        finally:
            await backend._client.aclose()

    asyncio.run(exercise())

    assert len(requests) == expected_requests
    if expected_requests == 1:
        assert chain._down_until[0] > time.time() + 45
    else:
        assert chain._down_until[0] <= time.time()

