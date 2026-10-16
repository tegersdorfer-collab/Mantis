"""Kurze Folgesätze in Timos Textkanälen übernehmen die Tools des vorherigen Turns.

Live 16.10., 15:29 (Web-Chat): „lampe wieder dunkler“ (Fast-Path) → „mach mehrmals“
→ leere Tool-Auswahl → „Ich kann die Lampe hier nicht tatsächlich steuern.“
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from communication.base import IncomingMessage
from core import fast_commands, message_handler
from tests import test_voice_carryover as carry
from tests.test_voice_carryover import _last_allowed, _turn

env = carry.env


def _text_turn(env, text, channel="web", text_followup_key=None):
    return asyncio.run(env.handler.dashboard_respond(
        text, agent=env.agent, channel_name=channel,
        text_followup_key=text_followup_key,
    ))


@pytest.fixture
def text_env(env, monkeypatch):
    monkeypatch.setattr(message_handler.fast_commands, "match", fast_commands.match)
    return env


@pytest.mark.parametrize("channel", ["web", "telegram", "discord"])
def test_live_flow_short_followup_inherits_fast_path_tool(text_env, channel):
    env = text_env
    reply, trace = _text_turn(env, "lampe wieder dunkler", channel)
    assert trace[0]["tool"] == "lampe"
    assert env.agent.calls == []

    env.clock.now += 20
    _text_turn(env, "mach mehrmals", channel)
    call = env.agent.calls[-1]
    assert "lampe" in call["allowed_tools"]
    assert call["force_tools"] is False


def test_followup_after_four_minutes_inherits_nothing(text_env):
    env = text_env
    _text_turn(env, "lampe wieder dunkler")
    env.clock.now += 240
    _text_turn(env, "mach mehrmals")
    assert "lampe" not in _last_allowed(env)


def test_followup_inherits_llm_turn_grant_and_chains(text_env):
    env = text_env
    _text_turn(env, "Erinnere mich heute Abend an Kreatin")
    env.clock.now += 30
    _text_turn(env, "und um neun")
    assert "set_reminder" in _last_allowed(env)
    assert env.agent.calls[-1]["force_tools"] is False


def test_long_sentence_does_not_inherit(text_env):
    env = text_env
    _text_turn(env, "lampe wieder dunkler")
    env.clock.now += 10
    _text_turn(env, "Erzähl mir bitte ganz ausführlich etwas über die Geschichte Roms")
    assert "lampe" not in _last_allowed(env)


def test_short_sentence_with_own_tools_does_not_inherit(text_env):
    env = text_env
    _text_turn(env, "lampe wieder dunkler")
    env.clock.now += 10
    _text_turn(env, "Wie wird das Wetter")
    assert "lampe" not in _last_allowed(env)


def test_async_own_selection_prevents_followup_inheritance(text_env, monkeypatch):
    env = text_env

    async def select_async(text):
        if text == "Brauch ich eine Jacke?":
            return ["get_weather"], False
        return [], False

    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    _text_turn(env, "lampe wieder dunkler")
    env.clock.now += 10
    _text_turn(env, "Brauch ich eine Jacke?")

    allowed = _last_allowed(env)
    assert "get_weather" in allowed
    assert "lampe" not in allowed


def test_full_catalog_async_selection_clears_keyword_toolless_candidate(
    text_env, monkeypatch,
):
    env = text_env
    monkeypatch.setattr(env.agent.backend, "sends_full_tool_catalog", True)

    async def select_async(text):
        if text == "Brauch ich eine Jacke?":
            return ["get_weather"], False
        return [], False

    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    _text_turn(env, "lampe wieder dunkler")
    env.clock.now += 10

    async def await_selection(kwargs):
        selected, force = await kwargs["tool_selection_task"]
        kwargs["allowed_tools"] = selected
        kwargs["force_tools"] = force
        return "Antwort.", []

    env.agent.behaviors.append(await_selection)
    _text_turn(env, "Brauch ich eine Jacke?")

    grant = env.handler._text_turn_grants[("web",)]
    assert "get_weather" in grant.names
    assert "lampe" not in grant.names


def test_channels_are_separate(text_env):
    env = text_env
    _text_turn(env, "lampe wieder dunkler", "web")
    env.clock.now += 10
    _text_turn(env, "mach mehrmals", "telegram")
    assert "lampe" not in _last_allowed(env)


def test_voice_rules_unchanged_by_text_followup(text_env):
    env = text_env
    _text_turn(env, "lampe wieder dunkler", "web")
    env.clock.now += 10
    _turn(env, "mach mehrmals")
    assert "lampe" not in _last_allowed(env)


def test_toolless_run_gets_ask_instead_of_cannot_hint(text_env):
    env = text_env
    _text_turn(env, "mach mehrmals")
    system = env.agent.calls[-1]["system"]
    assert message_handler.TOOLLESS_HINT in system
    assert "nicht" in message_handler.TOOLLESS_HINT
    _text_turn(env, "Wie wird das Wetter")
    assert message_handler.TOOLLESS_HINT not in env.agent.calls[-1]["system"]


@pytest.mark.parametrize(
    ("channel", "raw", "expected"),
    [
        (
            "discord",
            SimpleNamespace(channel=SimpleNamespace(id="thread-31")),
            ("discord", "user-7", "thread-31"),
        ),
        (
            "telegram",
            SimpleNamespace(effective_chat=SimpleNamespace(id=9021)),
            ("telegram", "user-7", "9021"),
        ),
    ],
)
def test_followup_scope_uses_ingress_sender_and_conversation(channel, raw, expected):
    msg = IncomingMessage(
        text="mach mehrmals", sender_id="user-7", timestamp=datetime(2026, 10, 16),
        raw=raw, channel=channel,
    )
    assert message_handler._text_followup_key_for_message(msg) == expected


@pytest.mark.parametrize(
    ("other_sender", "other_channel"),
    [("user-7", "guild-allowed"), ("user-8", "dm-7")],
)
def test_discord_followup_does_not_cross_sender_or_channel(
    text_env, other_sender, other_channel,
):
    env = text_env

    def discord_key(sender, channel_id):
        msg = IncomingMessage(
            text="", sender_id=sender, timestamp=datetime(2026, 10, 16),
            raw=SimpleNamespace(channel=SimpleNamespace(id=channel_id)),
            channel="discord",
        )
        return message_handler._text_followup_key_for_message(msg)

    message_handler.fast_commands.match = fast_commands.match
    _text_turn(
        env, "lampe wieder dunkler", "discord",
        discord_key("user-7", "dm-7"),
    )
    env.clock.now += 20
    _text_turn(
        env, "mach mehrmals", "discord",
        discord_key(other_sender, other_channel),
    )

    assert "lampe" not in _last_allowed(env)


def test_new_discord_inbox_input_consumes_previous_text_grant(text_env, monkeypatch):
    env = text_env

    response_channel = SimpleNamespace(
        send=AsyncMock(), send_typing=AsyncMock(), stop_typing=AsyncMock(),
        supports_streaming=False,
    )
    monkeypatch.setattr(message_handler.db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        message_handler.inbox_service, "capture_discord_message",
        AsyncMock(return_value={"response": "📥 Gemerkt."}),
    )
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)
    env.handler.gym_coach.classify = lambda *_args, **_kwargs: None

    async def incoming(text):
        msg = IncomingMessage(
            text=text, sender_id="user-7", timestamp=datetime(2026, 10, 16),
            raw=SimpleNamespace(channel=SimpleNamespace(id="dm-7")),
            channel="discord",
        )
        context = env.handler.begin_incoming_message(msg)
        await env.handler._handle_incoming(
            msg, response_channel=response_channel, turn_context=context,
        )

    asyncio.run(incoming("lampe wieder dunkler"))
    key = ("discord", "user-7", "dm-7")
    assert "lampe" in env.handler._text_turn_grants[key].names
    asyncio.run(incoming("https://example.org/article"))
    env.clock.now += 5
    asyncio.run(incoming("ja"))

    assert "lampe" not in _last_allowed(env)


def test_slow_discord_selection_cannot_restore_grant_after_new_inbox_input(
    text_env, monkeypatch,
):
    env = text_env
    response_channel = SimpleNamespace(
        send=AsyncMock(), send_typing=AsyncMock(), stop_typing=AsyncMock(),
        supports_streaming=False,
    )
    monkeypatch.setattr(message_handler.db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        message_handler.inbox_service, "capture_discord_message",
        AsyncMock(return_value={"response": "📥 Gemerkt."}),
    )
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)
    env.handler.gym_coach.classify = lambda *_args, **_kwargs: None
    selection_started = asyncio.Event()
    release_selection = asyncio.Event()

    async def select_tools(text):
        if text.startswith("Erinnere mich"):
            selection_started.set()
            await release_selection.wait()
            return ["set_reminder"], False
        return [], False

    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_tools)

    def discord_message(text):
        return IncomingMessage(
            text=text, sender_id="user-7", timestamp=datetime(2026, 10, 16),
            raw=SimpleNamespace(channel=SimpleNamespace(id="dm-7")),
            channel="discord",
        )

    async def incoming(text):
        msg = discord_message(text)
        context = env.handler.begin_incoming_message(msg)
        await env.handler._handle_incoming(
            msg, response_channel=response_channel, turn_context=context,
        )

    async def scenario():
        slow_selection = asyncio.create_task(
            incoming("Erinnere mich heute um neun an Kreatin"),
        )
        await asyncio.wait_for(selection_started.wait(), timeout=2)
        await incoming("https://example.org/article")
        release_selection.set()
        await asyncio.wait_for(slow_selection, timeout=2)
        await incoming("ja")

    asyncio.run(scenario())

    assert "set_reminder" not in _last_allowed(env)
