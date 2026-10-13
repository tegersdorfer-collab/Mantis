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


def test_extract_tool_calls_tolerates_broken_arguments():
    message = {"tool_calls": [{"id": "x", "function": {"name": "noop", "arguments": "{not json"}}]}

    assert _extract_tool_calls(message)[0]["function"]["arguments"] == {}


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
    asyncio.run(_backend(handler).call(
        [{"role": "user", "content": "hi"}], tools=None, stream_cb=None,
        temperature=0.5, max_tokens=10,
    ))

    assert seen["body"]["usage"] == {"include": True}
    assert records == [(("openrouter", "z-ai/glm-5.3", 12, 5), {
        "purpose": "chat-agent", "actual_cost": 0.001237,
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
