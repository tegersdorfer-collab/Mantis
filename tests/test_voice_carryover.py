"""Voice-Turns, die vor ihrer Ausführung abgebrochen wurden, tragen ihre Anfrage weiter.

Simuliert den Live-Fall vom 16.10.: Timo diktiert am Stück, die Pipeline schneidet
Segmente, frühere Turns werden abgebrochen, bevor ihre Tools liefen. Der nächste
Voice-Turn soll die offenen Anfragen mit vollen Rechten freigeben, kurze vage
Rückfragen nach abgeschlossenen Turns bleiben aber read-only.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from core import message_handler
from core.fast_commands import FastCommand
from core.message_handler import MessageHandler
from core.stream_events import StreamEvent

READ_ONLY = message_handler._VOICE_CONTEXT_READ_ONLY_TOOLS


def _fake_selection(text):
    low = text.casefold()
    names = []
    if "wetter" in low:
        names.append("get_weather")
    if "erinner" in low:
        names.append("set_reminder")
    if "licht" in low:
        names.append("lampe")
    if "spotify" in low:
        names.append("spotify")
    if "woche" in low or "überblick" in low:
        names.append("get_calendar")
    return names, any(name not in READ_ONLY for name in names)


class _KZG:
    def __init__(self):
        self.messages = []

    def add(self, role, content):
        self.messages.append({"role": role, "content": content})

    def recent_messages(self, **_kwargs):
        return [dict(message) for message in self.messages]


class _Agent:
    model_name = "fake"
    backend = type("Backend", (), {"sends_full_tool_catalog": False})()

    def __init__(self):
        self.calls = []
        self.behaviors = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0) if self.behaviors else None
        if callable(behavior):
            return await behavior(kwargs)
        return "Okay.", []


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    selections = []

    def keyword(text):
        selections.append(("keyword", text))
        return _fake_selection(text)

    async def select_async(text):
        selections.append(("async", text))
        return _fake_selection(text)

    executed = []

    async def execute(tool, args):
        executed.append((tool, args))
        return "Lampe gedimmt."

    monkeypatch.setattr(message_handler, "_voice_clock", clock)
    monkeypatch.setattr(message_handler, "_keyword_tool_selection", keyword)
    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    monkeypatch.setattr(message_handler.skills.T, "execute", execute)
    monkeypatch.setattr(message_handler.fast_commands, "match_voice", lambda _text: None)
    monkeypatch.setattr(message_handler.skill_request, "match", lambda _text: None)
    monkeypatch.setattr(message_handler, "_safe_task", lambda coro, _label: coro.close())

    agent = _Agent()
    prompt_builder = MagicMock()

    async def build(text, **_kwargs):
        return f"Kontext: {text}"

    prompt_builder.build = build
    alphaprogression = MagicMock()
    alphaprogression.extract_link = MagicMock(return_value=None)
    handler = MessageHandler(
        kzg=_KZG(), lzg=MagicMock(), agent=agent, prompt_builder=prompt_builder,
        channel=MagicMock(), proactive_tracker=MagicMock(),
        forgetting=MagicMock(), extractor=MagicMock(), bg_llm=MagicMock(),
        alphaprogression=alphaprogression, on_user_active=MagicMock(),
    )
    handler._persist_msg = MagicMock()

    class Env:
        pass

    e = Env()
    e.clock, e.handler, e.agent = clock, handler, agent
    e.selections, e.executed, e.monkeypatch = selections, executed, monkeypatch
    return e


async def _cancelled_before_tools(_kwargs):
    raise asyncio.CancelledError


def _turn(env, text, *, defer_finish=False, stream_cb=None):
    """Führt einen Voice-Turn aus; Abbrüche werden als 'cancelled' gemeldet."""
    async def run():
        try:
            return await env.handler.dashboard_respond(
                text, stream_cb=stream_cb, agent=env.agent, channel_name="voice",
                defer_finish=defer_finish,
            )
        except asyncio.CancelledError:
            return "cancelled"

    return asyncio.run(run())


def _last_allowed(env):
    return set(env.agent.calls[-1]["allowed_tools"])


FIRST = "Wie wird das Wetter morgen in Nürnberg und erinnere mich um neun an Kreatin"


def test_cancelled_turn_is_carried_into_next_voice_turn_with_full_rights(env):
    env.agent.behaviors.append(_cancelled_before_tools)
    assert _turn(env, FIRST) == "cancelled"

    env.clock.now += 14
    _turn(env, "Überblick bitte nicht zu ausführlich")

    allowed = _last_allowed(env)
    assert {"get_weather", "set_reminder"} <= allowed
    assert ("async", f"{FIRST}\nÜberblick bitte nicht zu ausführlich") in env.selections


def test_cancelled_turn_older_than_45_seconds_is_not_carried(env):
    env.agent.behaviors.append(_cancelled_before_tools)
    assert _turn(env, FIRST) == "cancelled"

    env.clock.now += 60
    _turn(env, "Überblick bitte nicht zu ausführlich")

    allowed = _last_allowed(env)
    assert "set_reminder" not in allowed
    assert "get_weather" not in allowed


def test_multiple_cancelled_turns_accumulate_and_completed_turn_clears_them(env):
    env.agent.behaviors.extend([_cancelled_before_tools, _cancelled_before_tools])
    assert _turn(env, FIRST) == "cancelled"
    env.clock.now += 4
    assert _turn(env, "und spiel irgendwas Ruhiges auf Spotify") == "cancelled"

    env.clock.now += 10
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert {"get_weather", "set_reminder", "spotify"} <= _last_allowed(env)

    # Der Turn ist regulär abgeschlossen: offene Anfragen sind erledigt.
    env.clock.now += 5
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert not ({"set_reminder", "spotify"} & _last_allowed(env))


def test_completed_turn_followup_stays_read_only(env):
    async def light_off(_kwargs):
        return "Licht ist aus.", [{"tool": "lampe", "args": {}, "result": "ok"}]

    env.agent.behaviors.append(light_off)
    _turn(env, "Mach das Licht aus")

    env.clock.now += 5
    _turn(env, "und morgen?")

    allowed = _last_allowed(env)
    assert "lampe" not in allowed
    assert allowed <= READ_ONLY


def test_retry_request_reuses_previous_voice_grant(env):
    async def blocked(_kwargs):
        return "Das geht gerade nicht.", [{
            "tool": "set_reminder", "args": {},
            "result": "FEHLER: Tool 'set_reminder' ist für diesen Satz nicht freigegeben.",
        }]

    env.agent.behaviors.append(blocked)
    _turn(env, "Erinnere mich heute Abend um neun an Kreatin")

    env.clock.now += 30
    _turn(env, "Mantis, probier's noch mal.")
    assert "set_reminder" in _last_allowed(env)
    assert env.agent.calls[-1]["force_tools"] is True


def test_retry_request_expires_after_120_seconds(env):
    env.agent.behaviors.append(
        lambda _kwargs: asyncio.sleep(0, result=("Nicht freigegeben.", [])),
    )
    _turn(env, "Erinnere mich heute Abend um neun an Kreatin")

    env.clock.now += 180
    _turn(env, "Probier's noch mal")
    assert "set_reminder" not in _last_allowed(env)


@pytest.mark.parametrize("text", [
    "Probier's noch mal", "probiers nochmal", "Versuch es noch mal.",
    "Nochmal", "Noch einmal bitte", "Mach nochmal", "Mantis, nochmal!",
    "versuch's noch einmal",
])
def test_retry_phrases_are_recognized(text):
    assert message_handler._is_voice_retry_request(text)


@pytest.mark.parametrize("text", [
    "Mach das Licht nochmal an", "Spiel das Lied noch mal", "Wie wird das Wetter",
    "", "Erzähl mir nochmal einen Witz",
])
def test_retry_detection_ignores_new_requests(text):
    assert not message_handler._is_voice_retry_request(text)


def test_tool_started_before_cancellation_is_not_granted_again(env):
    async def reminder_then_cancel(kwargs):
        await kwargs["stream_cb"](StreamEvent("tool_start", "a1", tool_name="set_reminder"))
        raise asyncio.CancelledError

    async def stream_cb(_event):
        return None

    env.agent.behaviors.append(reminder_then_cancel)
    assert _turn(env, FIRST, stream_cb=stream_cb) == "cancelled"

    env.clock.now += 10
    _turn(env, "Überblick bitte nicht zu ausführlich")
    allowed = _last_allowed(env)
    assert "get_weather" in allowed
    assert "set_reminder" not in allowed


def test_interrupted_fast_path_turn_keeps_rest_of_sentence_open(env):
    sentence = "Mach das Licht im Wohnzimmer etwas dunkler und spiel was Ruhiges auf Spotify"
    command = FastCommand(tool="lampe", args={"aktion": "dunkler"}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )

    reply, trace = _turn(env, sentence, defer_finish=True)
    assert env.executed == [("lampe", {"aktion": "dunkler"})]
    env.handler.record_interrupted_voice_turn("Lampe")

    env.clock.now += 10
    _turn(env, "Überblick bitte nicht zu ausführlich")
    allowed = _last_allowed(env)
    assert "spotify" in allowed
    assert "lampe" not in allowed


def test_finished_fast_path_turn_is_not_carried(env):
    sentence = "Mach das Licht dunkler und spiel was Ruhiges auf Spotify"
    command = FastCommand(tool="lampe", args={}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )
    _turn(env, sentence)

    env.clock.now += 10
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "spotify" not in _last_allowed(env)


def test_unauthorized_speculative_turn_is_not_carried(env):
    async def rejected():
        return False

    async def run():
        try:
            await env.handler.dashboard_respond(
                FIRST, agent=env.agent, channel_name="voice",
                execution_gate=rejected,
            )
        except asyncio.CancelledError:
            return "cancelled"

    env.agent.behaviors.append(_cancelled_before_tools)
    assert asyncio.run(run()) == "cancelled"

    env.clock.now += 5
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "set_reminder" not in _last_allowed(env)
