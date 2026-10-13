from types import SimpleNamespace
import asyncio

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


class _FakeStream:
    def __init__(self, text, final_message):
        self.text = text
        self.final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    @property
    def text_stream(self):
        async def chunks():
            if self.text:
                yield self.text
        return chunks()

    async def get_final_message(self):
        return self.final_message


class _FakeMessages:
    def __init__(self, stream):
        self._stream = stream

    def stream(self, **_kwargs):
        return self._stream


def _streaming_backend(final_message):
    backend = object.__new__(ClaudeBackend)
    backend._model = "claude-haiku-5-5"
    backend._purpose = "chat-agent"
    backend._client = SimpleNamespace(messages=_FakeMessages(_FakeStream(
        "partial text", final_message,
    )))
    backend._record_usage = lambda _response: None
    return backend


async def _noop_stream_callback(_text):
    pass


def test_haiku_stream_without_message_stop_or_stop_reason_falls_back():
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="partial text")],
        stop_reason=None,
    )

    with pytest.raises(RuntimeError, match="ohne Abschluss"):
        asyncio.run(_streaming_backend(final).call(
            [{"role": "user", "content": "hi"}], None, _noop_stream_callback, 0.2, 100,
        ))


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "max_tokens", "stop_sequence"])
def test_haiku_stream_accepts_complete_stop_reasons_including_max_tokens(stop_reason):
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="answer")],
        stop_reason=stop_reason,
    )

    result = asyncio.run(_streaming_backend(final).call(
        [{"role": "user", "content": "hi"}], None, _noop_stream_callback, 0.2, 100,
    ))

    assert result == ("answer", [])


def test_haiku_tool_use_rejects_missing_required_arguments():
    response = SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", id="toolu-1", name="lookup", input={"extra": "x"},
    )])
    tools = [{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "required": ["query"]},
    }}]

    with pytest.raises(ValueError, match="Pflichtargumente.*query"):
        _extract_tool_calls(response, tools)
