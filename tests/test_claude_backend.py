from types import SimpleNamespace
import asyncio
import json

import anthropic
import httpx
import pytest

from core.backends.claude import ClaudeBackend, _extract_tool_calls, _ollama_msgs_to_anthropic
from llm.claude import ClaudeProvider


def test_anthropic_model_allowlist_defaults_to_haiku_55(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ALLOWED_MODELS", raising=False)
    monkeypatch.delenv("CLAUDE_CHAT_MODEL", raising=False)

    from settings import MantisSettings

    settings = MantisSettings(_env_file=None)

    assert settings.ANTHROPIC_ALLOWED_MODELS == ["claude-haiku-5-5"]
    assert settings.CLAUDE_CHAT_MODEL == "claude-haiku-5-5"


@pytest.mark.parametrize("provider", [ClaudeBackend, ClaudeProvider])
def test_anthropic_constructors_reject_models_outside_allowlist(monkeypatch, provider):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_ALLOWED_MODELS", ["claude-haiku-5-5"], raising=False)

    with pytest.raises(ValueError, match="ANTHROPIC_ALLOWED_MODELS"):
        provider(model="claude-opus-4-6")


def test_backend_rechecks_allowlist_before_each_api_call(monkeypatch):
    import config

    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    monkeypatch.setattr(config, "ANTHROPIC_ALLOWED_MODELS", ["claude-opus-4-6"], raising=False)

    with pytest.raises(ValueError, match="ANTHROPIC_ALLOWED_MODELS"):
        asyncio.run(backend.call([], None, None, 0.2, 100))


def test_llm_provider_rechecks_allowlist_before_chat_and_stream(monkeypatch):
    import config

    provider = object.__new__(ClaudeProvider)
    provider._model = "claude-haiku-5-5"
    monkeypatch.setattr(config, "ANTHROPIC_ALLOWED_MODELS", ["claude-opus-4-6"], raising=False)

    with pytest.raises(ValueError, match="ANTHROPIC_ALLOWED_MODELS"):
        asyncio.run(provider.chat([]))

    async def consume_stream():
        return [chunk async for chunk in provider.stream([])]

    with pytest.raises(ValueError, match="ANTHROPIC_ALLOWED_MODELS"):
        asyncio.run(consume_stream())


def test_claude_tool_use_requires_object_arguments():
    response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="toolu-1", name="lookup", input=["bad"] )])

    with pytest.raises(ValueError, match="JSON-Objekt"):
        _extract_tool_calls(response)


def test_anthropic_messages_drop_empty_turns_merge_roles_and_keep_tool_results():
    system, messages = _ollama_msgs_to_anthropic([
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "  \n"},
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "\t"},
        {"role": "assistant", "content": "Answer"},
        {
            "role": "assistant",
            "content": " ",
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "lookup", "arguments": {"q": "x"}},
            }],
        },
        {"role": "tool", "content": "found", "tool_use_id": "call-1"},
        {"role": "user", "content": "Next"},
    ])

    assert system == "system prompt"
    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "Question"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Answer"},
            {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "found"},
            {"type": "text", "text": "Next"},
        ]},
    ]


def test_claude_usage_preserves_background_purpose(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    backend._purpose = "background"

    backend._record_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=12, output_tokens=4)))

    assert records == [(('claude', 'claude-haiku-5-5', 12, 4), {
        "purpose": "background",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    })]


def test_claude_usage_records_cache_token_counts(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    backend._purpose = "chat-agent"

    backend._record_usage(SimpleNamespace(usage=SimpleNamespace(
        input_tokens=12,
        output_tokens=4,
        cache_creation_input_tokens=800,
        cache_read_input_tokens=1200,
    )))

    assert records == [(('claude', 'claude-haiku-5-5', 12, 4), {
        "purpose": "chat-agent",
        "cache_creation_input_tokens": 800,
        "cache_read_input_tokens": 1200,
    })]


def test_haiku_55_cost_uses_explicit_estimated_price_table_entry():
    from core.llm_usage import cost_usd

    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == 6.0


def _sdk_message_events(
    *, include_message_stop, text_deltas=("answer",), stop_reason="end_turn",
):
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
        *[
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }
            for text in text_deltas
        ],
        {
            "type": "content_block_stop",
            "index": 0,
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
    ]
    if include_message_stop:
        events.append({"type": "message_stop"})
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


def _call_claude_with_sdk_events(
    *, include_message_stop, text_deltas=("answer",),
    stop_reason="end_turn", purpose="chat-agent",
):
    async def call():
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200,
            content=_sdk_message_events(
                include_message_stop=include_message_stop, text_deltas=text_deltas,
                stop_reason=stop_reason,
            ),
            headers={"content-type": "text/event-stream"},
        ))
        async with httpx.AsyncClient(transport=transport) as http_client:
            backend = object.__new__(ClaudeBackend)
            backend._model = "claude-haiku-5-5"
            backend._purpose = purpose
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


def test_haiku_stream_reports_exact_text_deltas():
    result, collected = _call_claude_with_sdk_events(
        include_message_stop=True, text_deltas=("Es ist son", "nig."),
    )

    assert result == ("Es ist sonnig.", [])
    assert collected == ["Es ist son", "nig."]


@pytest.mark.parametrize(
    ("text_deltas", "expected"),
    [
        (("Der erste Satz ist fertig. ", "Die Nacht davor waren 8"), "Der erste Satz ist fertig."),
        (("Dein Puls ist 53. ", "Die Nacht davor war"), "Dein Puls ist 53."),
    ],
)
def test_voice_stream_max_tokens_drops_incomplete_last_sentence(text_deltas, expected):
    result, collected = _call_claude_with_sdk_events(
        include_message_stop=True,
        text_deltas=text_deltas,
        stop_reason="max_tokens",
        purpose="voice",
    )

    assert result == (expected, [])
    assert collected == list(text_deltas)


@pytest.mark.parametrize(
    ("partial", "expected"),
    [
        ("Der erste Satz ist fertig. Die Nacht davor waren 8", "Der erste Satz ist fertig."),
        ("Dein Puls ist 53. Die Nacht davor war", "Dein Puls ist 53."),
    ],
)
def test_voice_nonstream_max_tokens_drops_incomplete_last_sentence(partial, expected):
    response_body = {
        "id": "msg_voice_limit",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": partial}],
        "model": "claude-haiku-5-5",
        "stop_reason": "max_tokens",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 10},
    }

    async def call():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_body),
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            backend = object.__new__(ClaudeBackend)
            backend._model = "claude-haiku-5-5"
            backend._purpose = "voice"
            backend._client = anthropic.AsyncAnthropic(
                api_key="test", http_client=http_client,
            )
            backend._record_usage = lambda _response: None
            return await backend.call(
                [{"role": "user", "content": "hi"}], None,
                None, 0.2, 500,
            )

    assert asyncio.run(call()) == (expected, [])


def test_haiku_tool_use_rejects_missing_required_arguments():
    response = SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", id="toolu-1", name="lookup", input={"extra": "x"},
    )])
    tools = [{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "required": ["query"]},
    }}]

    with pytest.raises(ValueError, match="Pflichtargumente.*query"):
        _extract_tool_calls(response, tools)


def _claude_request_payload(model, *, system=None, tools=None):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            backend = object.__new__(ClaudeBackend)
            backend._model = model
            backend._purpose = "chat-agent"
            backend._client = anthropic.AsyncAnthropic(api_key="test", http_client=http_client)
            backend._record_usage = lambda _response: None
            messages = [{"role": "user", "content": "hi"}]
            if system is not None:
                messages.insert(0, {"role": "system", "content": system})
            result = await backend.call(
                messages, tools, None, 0.2, 100,
            )
        return result

    assert asyncio.run(call()) == ("ok", [])
    return seen["body"]


def _claude_request_payloads(model, turns):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": f"msg_test_{len(bodies)}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            backend = object.__new__(ClaudeBackend)
            backend._model = model
            backend._purpose = "chat-agent"
            backend._client = anthropic.AsyncAnthropic(api_key="test", http_client=http_client)
            backend._record_usage = lambda _response: None
            for messages, tools in turns:
                assert await backend.call(messages, tools, None, 0.2, 100) == ("ok", [])

    asyncio.run(call())
    return bodies


def test_haiku_55_omits_unsupported_temperature_parameter():
    payload = _claude_request_payload("claude-haiku-5-5")

    assert "temperature" not in payload
    assert "thinking" not in payload


def test_unallowed_haiku_45_is_rejected_before_request():
    with pytest.raises(ValueError, match="ANTHROPIC_ALLOWED_MODELS"):
        _claude_request_payload("claude-haiku-4-5-20251001")


def test_cached_system_prefix_and_tools_are_marked_in_anthropic_request(monkeypatch):
    from core import tools as toolreg
    from core.prompt_builder import CacheableSystemPrompt

    first_tool = {"type": "function", "function": {
        "name": "lookup", "description": "Lookup data",
        "parameters": {"type": "object", "properties": {}},
    }}
    last_tool = {"type": "function", "function": {
        "name": "create", "description": "Create data",
        "parameters": {"type": "object", "properties": {}},
    }}
    monkeypatch.setattr(toolreg, "ollama_schemas", lambda: [first_tool, last_tool])
    system = CacheableSystemPrompt(
        "Stable instructions\n\nRemembered facts and current time",
        cache_prefix="Stable instructions",
    )

    payload = _claude_request_payload(
        "claude-haiku-5-5", system=system, tools=[first_tool, last_tool],
    )

    assert payload["system"] == [
        {
            "type": "text", "text": "Stable instructions",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\nRemembered facts and current time"},
    ]
    assert "".join(block["text"] for block in payload["system"]) == str(system)
    assert payload["tools"] == [
        {
            "name": "create", "description": "Create data",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "lookup", "description": "Lookup data",
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": {"type": "ephemeral"},
        },
    ]


def test_anthropic_cached_prefix_is_byte_identical_when_selected_tools_change(monkeypatch):
    from core import tools as toolreg
    from core.prompt_builder import CacheableSystemPrompt

    # Registry-Einfügereihenfolge absichtlich nicht alphabetisch.
    monkeypatch.setattr(toolreg, "REGISTRY", {
        "list_tasks": toolreg.Tool(
            name="list_tasks", description="List open tasks", parameters={}, handler=lambda: "",
        ),
        "get_weather": toolreg.Tool(
            name="get_weather", description="Get current weather",
            parameters={"city": {"type": "string"}}, handler=lambda city=None: "",
        ),
    })
    weather_tools = toolreg.ollama_schemas(only=["get_weather"])
    task_tools = toolreg.ollama_schemas(only=["list_tasks"])
    stable = "You are Mantis. Follow the user's request carefully."
    turns = [
        ([
            {"role": "system", "content": CacheableSystemPrompt(
                f"{stable}\n\nWeather turn context", cache_prefix=stable,
            )},
            {"role": "user", "content": "Wie ist das Wetter?"},
        ], weather_tools),
        ([
            {"role": "system", "content": CacheableSystemPrompt(
                f"{stable}\n\nTask turn context", cache_prefix=stable,
            )},
            {"role": "user", "content": "Welche Aufgaben sind offen?"},
        ], task_tools),
    ]

    weather_payload, task_payload = _claude_request_payloads("claude-haiku-5-5", turns)

    weather_cached_prefix = json.dumps(
        [weather_payload["tools"], weather_payload["system"][0]],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    task_cached_prefix = json.dumps(
        [task_payload["tools"], task_payload["system"][0]],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    assert weather_cached_prefix == task_cached_prefix
    assert [tool["name"] for tool in weather_payload["tools"]] == ["get_weather", "list_tasks"]
    assert weather_payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert weather_payload["messages"][0]["content"][0]["text"] == "Wie ist das Wetter?"
    assert task_payload["messages"][0]["content"][0]["text"] == "Welche Aufgaben sind offen?"
    assert weather_payload["system"][1]["text"] == "\n\nWeather turn context"
    assert task_payload["system"][1]["text"] == "\n\nTask turn context"


def _captured_create_kwargs(purpose, model="claude-haiku-5-5"):
    captured = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", thinking="hmm"),
                    SimpleNamespace(type="text", text="ok"),
                ],
                stop_reason="end_turn",
                usage=None,
            )

    backend = object.__new__(ClaudeBackend)
    backend._model = model
    backend._purpose = purpose
    backend._client = SimpleNamespace(messages=FakeMessages())
    result = asyncio.run(backend.call(
        [{"role": "user", "content": "hi"}], None, None, 0.2, 100,
    ))
    return captured, result


@pytest.mark.parametrize("purpose", ["voice", "fast"])
def test_fast_purposes_disable_thinking_and_set_low_effort(purpose):
    kwargs, result = _captured_create_kwargs(purpose)

    assert kwargs["thinking"] == {"type": "disabled"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert result == ("ok", [])  # Thinking-Blöcke werden ignoriert


@pytest.mark.parametrize("purpose", ["chat-agent", "background", "vision"])
def test_other_purposes_keep_default_thinking(purpose):
    kwargs, _ = _captured_create_kwargs(purpose)

    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_fast_thinking_setting_can_be_turned_off(monkeypatch):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_FAST_THINKING_DISABLED", False, raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_FAST_EFFORT", "medium", raising=False)
    kwargs, _ = _captured_create_kwargs("voice")

    assert "thinking" not in kwargs
    assert kwargs["output_config"] == {"effort": "medium"}
