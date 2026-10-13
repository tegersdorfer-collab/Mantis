"""Tests für das OpenRouter-Backend (OpenAI-kompatible Chat-Completions).

Der Agent-Loop übergibt Messages und Tool-Calls im Ollama-Format; das Backend
muss diese verlustfrei ins OpenAI-Format übersetzen und die Antwort wieder ins
einheitliche Backend-Format zurückholen. Kein echter Netzwerkzugriff: httpx
läuft über einen MockTransport.
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backends.openrouter import (  # noqa: E402
    OpenRouterBackend,
    _extract_tool_calls,
    _msgs_to_openai,
)


@pytest.fixture(autouse=True)
def _avoid_usage_database_writes(monkeypatch):
    from core import llm_usage
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: None)


def _backend(handler) -> OpenRouterBackend:
    transport = httpx.MockTransport(handler)
    return OpenRouterBackend(
        model="z-ai/glm-5.3",
        api_key="sk-or-test",
        client=httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1"),
    )


def _completion(content: str = "", tool_calls: list | None = None, usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "gen-1",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 5},
    }


# ── Message-Konvertierung ──────────────────────────────────────────────────


def test_tool_result_uses_openai_tool_call_id():
    msgs = [
        {"role": "system", "content": "Du bist Mantis."},
        {"role": "user", "content": "Wie spät?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "clock", "arguments": {"tz": "Europe/Berlin"}}}],
        },
        {"role": "tool", "content": "10:42", "name": "clock", "tool_use_id": "call_1"},
    ]

    converted = _msgs_to_openai(msgs)

    assert converted[0] == {"role": "system", "content": "Du bist Mantis."}
    assistant = converted[2]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["type"] == "function"
    # OpenAI verlangt die Argumente als JSON-String, nicht als dict.
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"tz": "Europe/Berlin"}
    tool = converted[3]
    assert tool == {"role": "tool", "content": "10:42", "tool_call_id": "call_1"}


def test_tool_call_without_id_gets_stable_generated_id():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "clock", "arguments": {}}}]},
        {"role": "tool", "content": "10:42", "name": "clock", "tool_use_id": ""},
    ]

    converted = _msgs_to_openai(msgs)

    generated = converted[0]["tool_calls"][0]["id"]
    assert generated
    assert converted[1]["tool_call_id"] == generated


def test_extract_tool_calls_parses_json_arguments():
    response = _completion(
        tool_calls=[{
            "id": "call_9",
            "type": "function",
            "function": {"name": "start_route", "arguments": '{"destination": "Bahnhof"}'},
        }]
    )

    calls = _extract_tool_calls(response["choices"][0]["message"])

    assert calls == [{"id": "call_9", "function": {"name": "start_route", "arguments": {"destination": "Bahnhof"}}}]


def test_missing_tool_arguments_rejects_registered_tool_with_required_field(monkeypatch):
    from core import tools as T

    monkeypatch.setattr(T, "REGISTRY", dict(T.REGISTRY))
    @T.register(
        "openrouter_required_probe", "Probe mit Pflichtfeld.",
        {"destination": {"type": "string"}}, ["destination"], "general",
    )
    async def openrouter_required_probe(destination: str) -> str:
        return destination

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(tool_calls=[{
            "id": "call-required",
            "type": "function",
            "function": {"name": "openrouter_required_probe"},
        }]))

    with pytest.raises(ValueError, match="Pflichtargumente"):
        asyncio.run(_backend(handler).call(
            [{"role": "user", "content": "Los"}],
            tools=T.ollama_schemas(only=["openrouter_required_probe"]),
            stream_cb=None, temperature=0.2, max_tokens=30,
        ))


def test_missing_tool_arguments_defaults_to_empty_object_when_no_fields_required():
    message = {"tool_calls": [{
        "id": "call-optional",
        "function": {"name": "optional_tool"},
    }]}
    tools = [{"type": "function", "function": {
        "name": "optional_tool",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }}]

    assert _extract_tool_calls(message, tools) == [{
        "id": "call-optional",
        "function": {"name": "optional_tool", "arguments": {}},
    }]


@pytest.mark.parametrize("arguments", ["{not json", "", "[]", "null", '"text"', "42"])
def test_extract_tool_calls_rejects_invalid_or_non_object_arguments(arguments):
    message = {"tool_calls": [{"id": "x", "function": {"name": "noop", "arguments": arguments}}]}

    with pytest.raises(ValueError, match="Tool-Argumente"):
        _extract_tool_calls(message)


# ── HTTP-Aufruf ────────────────────────────────────────────────────────────


def test_call_sends_openai_payload_and_returns_text():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(content="Route gestartet."))

    backend = _backend(handler)
    text, calls = asyncio.run(backend.call(
        [{"role": "user", "content": "Starte die Route"}],
        tools=[{"type": "function", "function": {"name": "start_route", "parameters": {"type": "object"}}}],
        stream_cb=None,
        temperature=0.3,
        max_tokens=200,
    ))

    assert text == "Route gestartet."
    assert calls == []
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer sk-or-test"
    body = seen["body"]
    assert body["model"] == "z-ai/glm-5.3"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 200
    assert body["tools"][0]["function"]["name"] == "start_route"
    assert "stream" not in body or body["stream"] is False


def test_usage_cost_is_requested_and_recorded_as_actual_openrouter_cost(monkeypatch):
    from core import llm_usage

    seen: dict = {}
    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(
            content="ok",
            usage={"prompt_tokens": 12, "completion_tokens": 5, "cost": 0.001237},
        ))

    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    backend = _backend(handler)
    asyncio.run(backend.call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
        temperature=0.5, max_tokens=10,
    ))

    assert seen["body"]["usage"] == {"include": True}
    assert records == [(("openrouter", "z-ai/glm-5.3", 12, 5), {
        "purpose": "chat-agent", "actual_cost": 0.001237,
        "generation_id": "gen-1", "api_key": "sk-or-test",
        "generation_client": backend._client,
    })]


def test_usage_cost_is_recorded_when_zero_tokens_have_actual_cost(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(
            content="ok", usage={"prompt_tokens": 0, "completion_tokens": 0, "cost": 0},
        ))

    backend = _backend(handler)
    asyncio.run(backend.call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
        temperature=0.5, max_tokens=10,
    ))

    assert records == [(('openrouter', 'z-ai/glm-5.3', 0, 0), {
        "purpose": "chat-agent", "actual_cost": 0.0,
        "generation_id": "gen-1", "api_key": "sk-or-test",
        "generation_client": backend._client,
    })]


def test_force_tool_call_sets_tool_choice_required():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "start_route", "arguments": "{}"}},
        ]))

    backend = _backend(handler)
    text, calls = asyncio.run(backend.call(
        [{"role": "user", "content": "los"}],
        tools=[{"type": "function", "function": {"name": "start_route", "parameters": {}}}],
        stream_cb=None, temperature=0.1, max_tokens=50, force_tool_call=True,
    ))

    assert seen["body"]["tool_choice"] == "required"
    assert calls[0]["function"]["name"] == "start_route"
    assert text == ""


def test_call_without_tools_omits_tools_and_tool_choice():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(content="ok"))

    asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
        temperature=0.5, max_tokens=10, force_tool_call=True,
    ))

    assert "tools" not in seen["body"]
    assert "tool_choice" not in seen["body"]


def test_http_error_raises_so_fallback_backend_can_take_over():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_backend(handler).call(
            [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
            temperature=0.5, max_tokens=10,
        ))


def test_streaming_collects_deltas_and_reports_progress():
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Route "}}]},
        {"choices": [{"delta": {"content": "gestartet."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    progress: list[str] = []

    async def stream_cb(text: str) -> None:
        progress.append(text)

    text, calls = asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "los"}], tools=None, stream_cb=stream_cb,
        temperature=0.5, max_tokens=50,
    ))

    assert text == "Route gestartet."
    assert calls == []
    assert progress[-1] == "Route gestartet."


def test_streaming_tolerates_null_usage_and_reads_usage_after_finish_reason(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    chunks = [
        {"id": "gen-usage-tail", "choices": [{"delta": {"content": "Antwort"}}], "usage": None},
        {"id": "gen-usage-tail", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"id": "gen-usage-tail", "choices": [], "usage": {
            "prompt_tokens": 120, "completion_tokens": 45, "cost": 0.035,
        }},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    backend = _backend(handler)
    text, calls = asyncio.run(backend.call(
        [{"role": "user", "content": "hi"}], tools=None,
        stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
    ))

    assert text == "Antwort"
    assert calls == []
    assert records == [(('openrouter', 'z-ai/glm-5.3', 120, 45), {
        "purpose": "chat-agent", "actual_cost": 0.035,
        "generation_id": "gen-usage-tail", "api_key": "sk-or-test",
        "generation_client": backend._client,
    })]


@pytest.mark.parametrize("finish_reason", ["error", "content_filter", "length"])
def test_streaming_rejects_non_success_finish_reason_after_reading_usage(monkeypatch, finish_reason):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    chunks = [
        {"id": "gen-failed-finish", "choices": [{
            "delta": {"content": "unfertig"}, "finish_reason": finish_reason,
        }]},
        {"id": "gen-failed-finish", "choices": [], "usage": {
            "prompt_tokens": 120, "completion_tokens": 45, "cost": 0.035,
        }},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    backend = _backend(handler)
    with pytest.raises(RuntimeError, match="finish_reason"):
        asyncio.run(backend.call(
            [{"role": "user", "content": "hi"}], tools=None,
            stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
        ))

    assert records[0][0] == ("openrouter", "z-ai/glm-5.3", 120, 45)
    assert records[0][1]["actual_cost"] == 0.035


def test_streaming_stops_at_done_and_ignores_later_events():
    chunks = [
        {"choices": [{"delta": {"content": "fertig"}}]},
    ]
    body = (
        "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
        + "data: [DONE]\n\n"
        + f'data: {json.dumps({"choices": [{"delta": {"content": " zu spät"}}]})}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    text, _ = asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=lambda _: asyncio.sleep(0),
        temperature=0.5, max_tokens=50,
    ))

    assert text == "fertig"


@pytest.mark.parametrize(
    "body",
    [
        'data: {"error":{"message":"provider failed"}}\n\n',
        "data: {not json}\n\n",
        'data: {"choices":[{"delta":{"content":"unfertig"}}]}\n\n',
    ],
    ids=["http-200-error-object", "broken-json", "eof-before-finish"],
)
def test_streaming_rejects_error_malformed_and_unfinished_responses(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(_backend(handler).call(
            [{"role": "user", "content": "hi"}], tools=None,
            stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
        ))


def test_streaming_accepts_finish_reason_without_done():
    chunk = {"choices": [{"delta": {"content": "vollständig"}, "finish_reason": "stop"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\n".encode(),
                              headers={"content-type": "text/event-stream"})

    text, calls = asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "hi"}], tools=None,
        stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
    ))

    assert text == "vollständig"
    assert calls == []


def test_stream_abort_records_usage_received_before_transport_error(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    chunk = {
        "id": "gen-partial",
        "choices": [{"delta": {"content": "partial"}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 4},
    }

    class AbortedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            raise httpx.ReadError("connection dropped")

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=AbortedStream(), headers={"content-type": "text/event-stream"})

    backend = _backend(handler)
    with pytest.raises(httpx.ReadError):
        asyncio.run(backend.call(
            [{"role": "user", "content": "hi"}], tools=None,
            stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
        ))

    assert records == [(('openrouter', 'z-ai/glm-5.3', 17, 4), {
        "purpose": "chat-agent", "actual_cost": None,
        "generation_id": "gen-partial", "api_key": "sk-or-test",
        "generation_client": backend._client,
    })]


def test_stream_cancellation_records_usage_received_before_timeout(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    chunk = {
        "id": "gen-timeout",
        "choices": [{"delta": {"content": "partial"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 2},
    }
    chunk_consumed = asyncio.Event()

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            chunk_consumed.set()
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=HangingStream(), headers={"content-type": "text/event-stream"})

    backend = _backend(handler)

    async def cancel_call():
        task = asyncio.create_task(backend.call(
            [{"role": "user", "content": "hi"}], tools=None,
            stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
        ))
        await asyncio.wait_for(chunk_consumed.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())

    assert records == [(('openrouter', 'z-ai/glm-5.3', 8, 2), {
        "purpose": "chat-agent", "actual_cost": None,
        "generation_id": "gen-timeout", "api_key": "sk-or-test",
        "generation_client": backend._client,
    })]


def test_streaming_merges_tool_call_argument_fragments():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_s", "type": "function", "function": {"name": "start_route", "arguments": '{"dest'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ination": "Bahnhof"}'}},
        ]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    async def stream_cb(text: str) -> None:
        pass

    text, calls = asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "los"}],
        tools=[{"type": "function", "function": {"name": "start_route", "parameters": {}}}],
        stream_cb=stream_cb, temperature=0.5, max_tokens=50,
    ))

    assert text == ""
    assert calls == [{"id": "call_s", "function": {"name": "start_route", "arguments": {"destination": "Bahnhof"}}}]


def test_reasoning_effort_low_is_sent_by_default():
    """GLM 5.3 auf OpenRouter erzwingt Reasoning; ohne Drossel frisst es das
    ganze max_tokens-Budget (Feldtest 22.09.2026: 1500 Tokens, 53 s, leere
    Antwort). effort=low → 1 Reasoning-Token, Tool-Call korrekt."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(content="ok"))

    asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None, temperature=0.5, max_tokens=10,
    ))

    assert seen["body"]["reasoning"] == {"effort": "low"}


def test_reasoning_effort_can_be_disabled_or_changed():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(content="ok"))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1")
    backend = OpenRouterBackend(model="z-ai/glm-5.3", api_key="k", client=client, reasoning_effort="")
    asyncio.run(backend.call([{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
                             temperature=0.5, max_tokens=10))
    assert "reasoning" not in seen["body"]

    backend = OpenRouterBackend(model="z-ai/glm-5.3", api_key="k", client=client, reasoning_effort="medium")
    asyncio.run(backend.call([{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
                             temperature=0.5, max_tokens=10))
    assert seen["body"]["reasoning"] == {"effort": "medium"}


def test_model_name_and_requires_api_key():
    backend = OpenRouterBackend(model="z-ai/glm-5.3", api_key="k")
    assert backend.model_name == "z-ai/glm-5.3"
    with pytest.raises(ValueError):
        OpenRouterBackend(model="z-ai/glm-5.3", api_key="")


def test_api_key_is_trimmed_and_rejects_embedded_line_breaks():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=_completion(content="ok"))

    backend = OpenRouterBackend(
        model="z-ai/glm-5.3", api_key=" \tsynthetic-key\n ",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.ai/api/v1",
        ),
    )
    asyncio.run(backend.call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
        temperature=0.5, max_tokens=10,
    ))

    assert seen["authorization"] == "Bearer synthetic-key"
    with pytest.raises(ValueError, match="API-Key ist ungültig") as exc_info:
        OpenRouterBackend(model="z-ai/glm-5.3", api_key="synthetic\nkey")
    assert "synthetic" not in str(exc_info.value)
