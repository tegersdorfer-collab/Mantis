"""Kurze Folgesätze in Timos Textkanälen übernehmen die Tools des vorherigen Turns.

Live 16.10., 15:29 (Web-Chat): „lampe wieder dunkler“ (Fast-Path) → „mach mehrmals“
→ leere Tool-Auswahl → „Ich kann die Lampe hier nicht tatsächlich steuern.“
"""
import asyncio

import pytest

from core import fast_commands, message_handler
from tests import test_voice_carryover as carry
from tests.test_voice_carryover import _last_allowed, _turn

env = carry.env


def _text_turn(env, text, channel="web"):
    return asyncio.run(env.handler.dashboard_respond(
        text, agent=env.agent, channel_name=channel,
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
