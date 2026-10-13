"""Fehler aus dem Agent-Turn dürfen keine konfigurierten Schlüssel loggen."""
import asyncio
from types import SimpleNamespace

import config
from core import message_handler as message_handler_module


def test_agent_turn_redacts_synthetic_api_key_in_application_log(monkeypatch, caplog):
    synthetic_key = "sk-or-synthetic-agent-error-key"
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", synthetic_key, raising=False)
    monkeypatch.setattr(message_handler_module.fast_commands, "match", lambda _: None)
    monkeypatch.setattr(message_handler_module.skill_request, "match", lambda _: None)
    monkeypatch.setattr(message_handler_module.db, "log_error", lambda *_: None)

    async def select_tools(_):
        return [], False

    monkeypatch.setattr(message_handler_module.skills.T, "select_tools_async", select_tools)

    class PromptBuilder:
        async def build(self, _):
            return "system prompt"

    class Agent:
        async def run(self, **kwargs):
            raise RuntimeError(f"provider rejected key {synthetic_key}")

    handler = message_handler_module.MessageHandler(
        SimpleNamespace(recent_messages=lambda **_: []), None,
        Agent(), PromptBuilder(), None, None, None, None, None, None, lambda: None,
    )

    with caplog.at_level("ERROR", logger="core.message_handler"):
        result = asyncio.run(handler._agent_turn("hi", channel_name="web"))

    assert result == ("⚠️ Technisches Problem, versuch es nochmal.", [])
    assert synthetic_key not in caplog.text
    assert "[REDACTED]" in caplog.text
