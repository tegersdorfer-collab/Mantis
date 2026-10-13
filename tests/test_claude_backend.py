from types import SimpleNamespace

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
