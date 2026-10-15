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


def _response(*, text="", function_call=None, usage=None, finish_reason="STOP", parts=None):
    if parts is None:
        parts = []
        if text:
            parts.append({"text": text})
        if function_call is not None:
            parts.append({"functionCall": function_call})
    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": finish_reason,
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


@pytest.mark.parametrize("streaming", [False, True])
def test_thought_signatures_round_trip_on_their_original_parts(streaming):
    original_parts = [
        {"text": "Ich suche.", "thoughtSignature": "text-signature-1"},
        {"functionCall": {
            "id": "signed-call", "name": "lookup", "args": {"query": "Mantis"},
        }, "thoughtSignature": "call-signature-2"},
    ]

    def handler(_request):
        response = _response(parts=original_parts)
        body = json.dumps(response)
        if streaming:
            body = f"data: {body}\n\n"
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=response)

    backend = _backend(handler)

    async def discard(_text):
        return None

    text, calls = _call(
        backend,
        messages=[{"role": "user", "content": "Suche Mantis."}],
        tools=[{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
        stream_cb=discard if streaming else None,
    )
    followup = backend._payload([
        {"role": "user", "content": "Suche Mantis."},
        {"role": "assistant", "content": text, "tool_calls": calls},
        {"role": "tool", "name": "lookup", "tool_use_id": "signed-call", "content": "Treffer"},
    ], None, 0.2, 100, False)

    assert text == "Ich suche."
    assert calls == [{
        "id": "signed-call", "tool_use_id": "signed-call",
        "function": {"name": "lookup", "arguments": {"query": "Mantis"}},
        "thought_signature": "call-signature-2",
    }]
    assert followup["contents"] == [
        {"role": "user", "parts": [{"text": "Suche Mantis."}]},
        {"role": "model", "parts": original_parts},
        {"role": "user", "parts": [{"functionResponse": {
            "id": "signed-call", "name": "lookup", "response": {"result": "Treffer"},
        }}]},
    ]


@pytest.mark.parametrize("streaming", [False, True])
def test_text_only_thought_signature_is_kept_for_the_next_request(streaming):
    original_parts = [{"text": "Antwort.", "thoughtSignature": "text-only-signature"}]

    def handler(_request):
        response = _response(parts=original_parts)
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    backend = _backend(handler)

    async def discard(_text):
        return None

    text, calls = _call(
        backend,
        messages=[{"role": "user", "content": "Erkläre das."}],
        stream_cb=discard if streaming else None,
    )
    followup = backend._payload([
        {"role": "user", "content": "Erkläre das."},
        {"role": "assistant", "content": text.strip()},
        {"role": "user", "content": "Weiter."},
    ], None, 0.2, 100, False)

    assert text == "Antwort."
    assert calls == []
    assert followup["contents"][1] == {"role": "model", "parts": original_parts}


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


def test_streaming_keeps_each_complete_function_call_in_input_order():
    events = [
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {
            "name": "lookup", "args": {"query": "first"},
        }}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {
            "name": "lookup", "args": {"query": "second"},
        }}]}, "finishReason": "STOP"}]},
    ]

    def handler(_request):
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    backend = _backend(handler)
    text, calls = _call(
        backend,
        messages=[{"role": "user", "content": "Führe beide Suchen aus."}],
        tools=[{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
        stream_cb=lambda _text: asyncio.sleep(0),
    )

    assert text == ""
    assert len(calls) == 2
    assert all(call["id"] for call in calls)
    assert calls[0]["id"] != calls[1]["id"]
    assert [call["function"]["arguments"] for call in calls] == [
        {"query": "first"}, {"query": "second"},
    ]


def test_internal_message_parts_keep_signatures_when_rebuilt():
    from core.backends.gemini import _history_to_gemini

    _system, contents = _history_to_gemini([
        {"role": "assistant", "content": [
            {"type": "text", "text": "Ich suche.", "thought_signature": "text-part-signature"},
            {"type": "text", "text": "", "thought_signature": "empty-text-signature"},
        ], "tool_calls": [{
            "id": "call-8", "thought_signature": "function-part-signature",
            "function": {"name": "lookup", "arguments": {"query": "Mantis"}},
        }]},
    ])

    assert contents == [{"role": "model", "parts": [
        {"text": "Ich suche.", "thoughtSignature": "text-part-signature"},
        {"text": "", "thoughtSignature": "empty-text-signature"},
        {"functionCall": {
            "id": "call-8", "name": "lookup", "args": {"query": "Mantis"},
        }, "thoughtSignature": "function-part-signature"},
    ]}]


@pytest.mark.parametrize("streaming", [False, True])
def test_missing_required_gemini_tool_arguments_fail(streaming):
    response = _response(function_call={"id": "required-call", "name": "lookup"})

    def handler(_request):
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    backend = _backend(handler)
    with pytest.raises(ValueError, match="Pflichtargumente"):
        _call(
            backend,
            messages=[{"role": "user", "content": "Suche."}],
            tools=[{"type": "function", "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            }}],
            stream_cb=(lambda _text: asyncio.sleep(0)) if streaming else None,
        )


def test_tool_schema_conversion_sanitizes_nested_openapi_extensions_and_resolves_refs():
    from core.backends.gemini import _function_declarations

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "$defs": {"Contact": {
            "type": "object", "properties": {"email": {"type": "string", "default": "x@example.test"}},
        }},
        "definitions": {"LegacyName": {"type": "string", "examples": ["Timo"]}},
        "properties": {
            "contact": {"$ref": "#/$defs/Contact"},
            "legacy_name": {"$ref": "#/definitions/LegacyName"},
            "state": {"oneOf": [
                {"const": "open", "description": "Offen"},
                {"type": "string", "default": "closed", "examples": ["closed"]},
            ]},
            "filter": {"anyOf": [
                {"type": "string", "title": "Suchtext"},
                {"type": "integer", "examples": [1]},
            ]},
            "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["contact"],
        "examples": [{"contact": {}}],
    }
    converted = _function_declarations([{
        "type": "function", "function": {"name": "schema_probe", "parameters": schema},
    }])

    assert converted == [{"name": "schema_probe", "parameters": {
        "type": "object",
        "properties": {
            "contact": {"type": "object", "properties": {"email": {"type": "string"}}},
            "legacy_name": {"type": "string"},
            "state": {"anyOf": [
                {"enum": ["open"], "description": "Offen"},
                {"type": "string"},
            ]},
            "filter": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "metadata": {"type": "object"},
        },
        "required": ["contact"],
    }}]


def test_nested_log_workout_detailed_schema_remains_valid_after_conversion():
    from core.backends.gemini import _function_declarations

    # Dieses verschachtelte Schema entspricht core/skills/fitness.py:log_workout_detailed.
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "exercises": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "sets": {"type": "array", "items": {"type": "object", "properties": {
                    "reps": {"type": "integer"}, "weight_kg": {"type": "number"},
                }}},
            }}},
        },
        "required": ["title", "exercises"],
    }

    assert _function_declarations([{"name": "log_workout_detailed", "parameters": schema}]) == [{
        "name": "log_workout_detailed", "parameters": schema,
    }]


@pytest.mark.parametrize("finish_reason", [
    "SAFETY", "RECITATION", "MALFORMED_FUNCTION_CALL", "BLOCKLIST",
    "PROHIBITED_CONTENT", "SPII", "OTHER", "FINISH_REASON_UNSPECIFIED",
    "IMAGE_PROHIBITED_CONTENT", "NO_IMAGE",
])
@pytest.mark.parametrize("streaming", [False, True])
def test_unsupported_finish_reasons_are_errors(finish_reason, streaming):
    response = _response(text="Unfertig.", finish_reason=finish_reason)

    def handler(_request):
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    with pytest.raises(RuntimeError, match="finishReason"):
        _call(
            _backend(handler),
            messages=[{"role": "user", "content": "Antworte."}],
            stream_cb=(lambda _text: asyncio.sleep(0)) if streaming else None,
        )


@pytest.mark.parametrize("streaming", [False, True])
def test_max_tokens_without_text_is_not_a_success(streaming):
    response = _response(finish_reason="MAX_TOKENS")

    def handler(_request):
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        _call(
            _backend(handler),
            messages=[{"role": "user", "content": "Antworte."}],
            stream_cb=(lambda _text: asyncio.sleep(0)) if streaming else None,
        )


@pytest.mark.parametrize("streaming", [False, True])
def test_max_tokens_with_text_is_an_accepted_partial_answer(streaming):
    response = _response(text="Teilantwort.", finish_reason="MAX_TOKENS")

    def handler(_request):
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    text, calls = _call(
        _backend(handler),
        messages=[{"role": "user", "content": "Antworte."}],
        stream_cb=(lambda _text: asyncio.sleep(0)) if streaming else None,
    )

    assert text == "Teilantwort."
    assert calls == []


def test_stream_without_finish_reason_or_done_fails_completion_check():
    partial = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Teil"}]}}]}

    def handler(_request):
        return httpx.Response(
            200,
            content=f"data: {json.dumps(partial)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(RuntimeError, match="ohne Abschluss"):
        _call(
            _backend(handler),
            messages=[{"role": "user", "content": "Antworte."}],
            stream_cb=lambda _text: asyncio.sleep(0),
        )


def test_stream_finish_reason_error_aborts_attempt_and_falls_back():
    response = _response(text="Blockiert.", finish_reason="SAFETY")

    def handler(_request):
        return httpx.Response(
            200,
            content=f"data: {json.dumps(response)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    backend = _backend(handler)

    class Healthy:
        model_name = "fallback-model"

        async def call(self, **_kwargs):
            return "Fallback-Antwort", []

    chain = FallbackBackend(backends=[backend, Healthy()], stage_timeouts=[2, 2], cooldown_s=0)
    events = []

    async def collect(event):
        events.append(event)

    async def exercise():
        try:
            return await Agent(chain).run(
                messages=[{"role": "user", "content": "Antworte."}],
                system="", stream_cb=collect, use_tools=False, max_tokens=30,
            )
        finally:
            await backend._client.aclose()

    assert asyncio.run(exercise()) == ("Fallback-Antwort", [])
    assert [event.kind for event in events] == [
        "attempt_start", "delta", "attempt_abort", "attempt_start", "attempt_end",
    ]


@pytest.mark.parametrize("streaming", [False, True])
def test_thought_token_count_is_added_to_output_usage_and_cost(monkeypatch, streaming):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    monkeypatch.setattr("config.GEMINI_USD_PER_MTOK_IN", 0.5, raising=False)
    monkeypatch.setattr("config.GEMINI_USD_PER_MTOK_OUT", 2.0, raising=False)
    usage = {
        "promptTokenCount": 10, "candidatesTokenCount": 5,
        "thoughtsTokenCount": 7, "totalTokenCount": 22,
    }

    def handler(_request):
        response = _response(text="Ergebnis.", usage=usage)
        if streaming:
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    _call(
        _backend(handler),
        messages=[{"role": "user", "content": "Antworte."}],
        stream_cb=(lambda _text: asyncio.sleep(0)) if streaming else None,
    )

    assert records == [(("gemini", "gemini-3-flash-test", 10, 12), {
        "purpose": "chat-agent", "actual_cost": pytest.approx(0.000029),
    })]


def test_custom_gemini_api_key_is_redacted_from_error_message():
    direct_key = "gemini-direct-custom-key"

    def handler(_request):
        return httpx.Response(400, json={"error": {
            "code": 400, "status": "INVALID_ARGUMENT",
            "message": f"Rejected key {direct_key}",
        }})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://aiplatform.googleapis.com",
    )
    backend = GeminiBackend(
        model="gemini-3-flash-test", api_key=direct_key, client=client,
    )
    with pytest.raises(RuntimeError) as raised:
        _call(backend, messages=[{"role": "user", "content": "hi"}])

    assert direct_key not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


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

