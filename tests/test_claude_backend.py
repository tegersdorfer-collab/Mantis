from types import SimpleNamespace
import asyncio
import json

import anthropic
import httpx
import pytest

from core.backends.claude import ClaudeBackend, _extract_tool_calls


def test_claude_tool_use_requires_object_arguments():
    response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="toolu-1", name="lookup", input=["bad"] )])

    with pytest.raises(ValueError, match="JSON-Objekt"):
        _extract_tool_calls(response)


def test_claude_usage_preserves_background_purpose(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    backend._purpose = "background"

    backend._record_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=12, output_tokens=4)))

    assert records == [(('claude', 'claude-haiku-5-5', 12, 4), {"purpose": "background"})]


def test_haiku_55_cost_uses_explicit_estimated_price_table_entry():
    from core.llm_usage import cost_usd

    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == 6.0


def _sdk_message_events(*, include_message_stop):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "answer"},
        },
        {
            "type": "content_block_stop",
            "index": 0,
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
    ]
    if include_message_stop:
        events.append({"type": "message_stop"})
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


def _call_claude_with_sdk_events(*, include_message_stop):
    async def call():
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200,
            content=_sdk_message_events(include_message_stop=include_message_stop),
            headers={"content-type": "text/event-stream"},
        ))
        async with httpx.AsyncClient(transport=transport) as http_client:
            backend = object.__new__(ClaudeBackend)
            backend._model = "claude-haiku-5-5"
            backend._purpose = "chat-agent"
            backend._client = anthropic.AsyncAnthropic(
                api_key="test", http_client=http_client,
            )
            backend._record_usage = lambda _response: None
            collected = []

            async def stream_callback(text):
                collected.append(text)

            result = await backend.call(
                [{"role": "user", "content": "hi"}], None,
                stream_callback, 0.2, 100,
            )
            return result, collected

    return asyncio.run(call())


def test_haiku_stream_without_message_stop_falls_back_even_with_stop_reason():
    with pytest.raises(RuntimeError, match="message_stop"):
        _call_claude_with_sdk_events(include_message_stop=False)


def test_haiku_stream_accepts_sdk_stream_ending_with_message_stop():
    result, collected = _call_claude_with_sdk_events(include_message_stop=True)

    assert result == ("answer", [])
    assert collected == ["answer"]


def test_haiku_tool_use_rejects_missing_required_arguments():
    response = SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", id="toolu-1", name="lookup", input={"extra": "x"},
    )])
    tools = [{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "required": ["query"]},
    }}]

    with pytest.raises(ValueError, match="Pflichtargumente.*query"):
        _extract_tool_calls(response, tools)
