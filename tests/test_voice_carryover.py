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
    # S1: Force nie aus Retry/übernommenem Text, nur aus dem aktuellen Satz.
    assert env.agent.calls[-1]["force_tools"] is False


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
    "versuch's noch einmal", "kannst du es nochmal versuchen", "nochmal probieren",
    "versuch's nochmal, Mantis",
])
def test_retry_phrases_are_recognized(text):
    assert message_handler._is_voice_retry_request(text)


@pytest.mark.parametrize("text", [
    "Mach das Licht nochmal an", "Spiel das Lied noch mal", "Wie wird das Wetter",
    "", "Erzähl mir nochmal einen Witz", "schick die Nachricht nochmal",
    "spiel das nochmal", "nicht nochmal",
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


def _success(tool):
    async def run(_kwargs):
        return "Erledigt.", [{"tool": tool, "args": {}, "result": "ok"}]
    return run


def test_carry_turn_completing_agent_run_clears_open_list_even_if_playback_interrupted(env):
    """P1: B erledigt A, Playback von B wird unterbrochen → A darf nicht offen bleiben."""
    env.agent.behaviors.extend([_cancelled_before_tools, _success("set_reminder")])
    assert _turn(env, "Erinnere mich um neun an Kreatin") == "cancelled"
    env.clock.now += 3
    _turn(env, "mit Ton bitte", defer_finish=True)
    assert "set_reminder" in _last_allowed(env)
    env.handler.record_interrupted_voice_turn("Erle")

    env.clock.now += 5
    _turn(env, "danke dir")
    assert "set_reminder" not in _last_allowed(env)
    assert env.agent.calls[-1]["force_tools"] is False


def test_carry_turn_without_playback_end_still_clears_open_list(env):
    """P1: Timeout/Disconnect – weder finish noch interrupt kommen."""
    env.agent.behaviors.extend([_cancelled_before_tools, _success("set_reminder")])
    assert _turn(env, "Erinnere mich um neun an Kreatin") == "cancelled"
    env.clock.now += 3
    _turn(env, "mit Ton bitte", defer_finish=True)

    env.clock.now += 5
    _turn(env, "danke dir")
    assert "set_reminder" not in _last_allowed(env)


def test_interrupt_without_turn_id_ignores_stale_deferred_turn(env):
    """P2a: alter, nie abgeschlossener Turn darf nicht dem neuen Abbruch zugeordnet werden."""
    _turn(env, "Spiel was auf Spotify", defer_finish=True)
    env.clock.now += 30
    env.agent.behaviors.append(_cancelled_before_tools)
    assert _turn(env, "Wie wird das Wetter") == "cancelled"
    env.handler.record_interrupted_voice_turn("")

    ledger = env.handler._voice_ledger()
    assert ledger.last.text == "Wie wird das Wetter"


def test_interrupt_and_finish_are_matched_by_turn_id(env):
    """P2a: Router meldet Turn-IDs; Unterbrechung trifft genau den gemeldeten Turn."""
    sentence = "Mach das Licht dunkler und spiel was auf Spotify"
    command = FastCommand(tool="lampe", args={}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )

    async def run():
        await env.handler.dashboard_respond(
            sentence, agent=env.agent, channel_name="voice",
            defer_finish=True, voice_turn_id="turn-a",
        )
        env.clock.now += 2
        await env.handler.dashboard_respond(
            "Wie spät ist es", agent=env.agent, channel_name="voice",
            defer_finish=True, voice_turn_id="turn-b",
        )

    asyncio.run(run())
    # Die Unterbrechung von A kommt erst nach dem Lauf von B an.
    env.handler.record_interrupted_voice_turn("Lampe", turn_id="turn-a")
    env.handler._finish_turn(
        "Wie spät ist es", "Zehn.", [], channel_name="voice", turn_id="turn-b",
    )

    env.clock.now += 3
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "spotify" in _last_allowed(env)
    assert "lampe" not in _last_allowed(env)


def test_dispatch_gate_blocks_carried_tool_already_started_by_parallel_turn(env):
    """P2b: C wählt Tools, während B die Anfrage A schon ausführt."""
    b_started = asyncio.Event()
    release_b = asyncio.Event()
    gate_results = {}

    async def b_runs_reminder(kwargs):
        gate_results["b"] = kwargs["tool_gate"]("set_reminder")
        b_started.set()
        await release_b.wait()
        return "Erledigt.", [{"tool": "set_reminder", "args": {}, "result": "ok"}]

    async def c_tries_reminder(kwargs):
        gate_results["c"] = kwargs["tool_gate"]("set_reminder")
        gate_results["c_weather"] = kwargs["tool_gate"]("get_weather")
        return "Okay.", []

    env.agent.behaviors.extend([_cancelled_before_tools, b_runs_reminder, c_tries_reminder])
    assert _turn(env, FIRST) == "cancelled"

    async def run():
        env.clock.now += 3
        b = asyncio.create_task(env.handler.dashboard_respond(
            "mit Ton bitte", agent=env.agent, channel_name="voice",
        ))
        await asyncio.wait_for(b_started.wait(), timeout=2)
        env.clock.now += 2
        await env.handler.dashboard_respond(
            "Überblick bitte nicht zu ausführlich", agent=env.agent, channel_name="voice",
        )
        release_b.set()
        await b

    asyncio.run(run())
    assert gate_results["b"] is None
    assert "set_reminder" in set(env.agent.calls[2]["allowed_tools"])
    assert gate_results["c"] is not None and "bereits" in gate_results["c"]
    assert gate_results["c_weather"] is None


@pytest.mark.parametrize("cancel_text", [
    "Ja okay, dann lass es. Stop.", "Stopp", "Nein, vergiss es", "doch nicht",
    "Warte mal", "abbrechen", "Das meinte ich nicht",
])
def test_cancel_phrase_clears_open_requests_and_retry(env, cancel_text):
    """S2: Nach einem Widerruf wird nichts mehr nachgeholt."""
    env.agent.behaviors.append(_cancelled_before_tools)
    assert _turn(env, FIRST) == "cancelled"
    env.clock.now += 3
    _turn(env, cancel_text)
    assert "set_reminder" not in _last_allowed(env)

    env.clock.now += 3
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "set_reminder" not in _last_allowed(env)
    env.clock.now += 3
    _turn(env, "Probier's noch mal")
    assert "set_reminder" not in _last_allowed(env)


def test_cancel_phrase_during_interrupted_fast_path_keeps_rest_closed(env):
    """S2: Die Unterbrechung des Fast-Path-Turns kommt nach dem Widerruf an."""
    sentence = "Mach das Licht dunkler und spiel was auf Spotify"
    command = FastCommand(tool="lampe", args={}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )
    _turn(env, sentence, defer_finish=True)
    env.clock.now += 2
    _turn(env, "Stopp, lass es")
    env.handler.record_interrupted_voice_turn("Lampe")

    env.clock.now += 2
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "spotify" not in _last_allowed(env)


def test_carried_text_never_forces_tool_call(env):
    """S1: Übernommener Text mit Aktions-Tools setzt kein force."""
    env.agent.behaviors.append(_cancelled_before_tools)
    assert _turn(env, FIRST) == "cancelled"
    env.clock.now += 3
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "set_reminder" in _last_allowed(env)
    assert env.agent.calls[-1]["force_tools"] is False


def test_fast_path_cancelled_during_execution_is_not_retried(env):
    """P3a: Abbruch während skills.T.execute → Tool zählt als gestartet."""
    sentence = "Mach das Licht aus"
    command = FastCommand(tool="lampe", args={}, label="lampe-aus")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )

    async def hanging_execute(_tool, _args):
        raise asyncio.CancelledError

    env.monkeypatch.setattr(message_handler.skills.T, "execute", hanging_execute)
    assert _turn(env, sentence) == "cancelled"

    env.clock.now += 5
    _turn(env, "Probier's noch mal")
    assert "lampe" not in _last_allowed(env)


def test_retry_does_not_regrant_successfully_executed_tool(env):
    """Mutationen: retry_grant ignoriert executed / record_trace zählt Erfolg nicht."""
    env.agent.behaviors.append(_success("set_reminder"))
    _turn(env, "Erinnere mich heute Abend um neun an Kreatin")

    env.clock.now += 10
    _turn(env, "Probier's noch mal")
    assert "set_reminder" not in _last_allowed(env)


def test_record_trace_counts_only_successful_tools_as_executed():
    state = message_handler._VoiceTurnState(text="x", started_at=0.0)
    state.record_trace([
        {"tool": "set_reminder", "result": "Erinnerung gesetzt."},
        {"tool": "spotify", "result": "FEHLER: Tool 'spotify' ist nicht freigegeben."},
        {"tool": "lampe", "result": "❌ lampe fehlgeschlagen"},
    ])
    assert state.executed == {"set_reminder"}


def test_failed_interrupted_fast_path_tool_is_not_carried_again(env):
    """Mutation: reflex_tools aus carry_excluded entfernt → Lampe würde erneut freigegeben."""
    sentence = "Mach das Licht dunkler und spiel was auf Spotify"
    command = FastCommand(tool="lampe", args={}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )

    async def failing_execute(_tool, _args):
        return "❌ lampe fehlgeschlagen: keine Hardware"

    env.monkeypatch.setattr(message_handler.skills.T, "execute", failing_execute)
    _turn(env, sentence, defer_finish=True)
    env.handler.record_interrupted_voice_turn("Lampe")

    env.clock.now += 5
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "spotify" in _last_allowed(env)
    assert "lampe" not in _last_allowed(env)


def test_router_passes_turn_id_only_to_receivers_that_accept_it():
    import web.routers.voice as voice_router

    calls = []

    def legacy(spoken_text):
        calls.append(("legacy", spoken_text))

    def modern(spoken_text, turn_id=None):
        calls.append(("modern", spoken_text, turn_id))

    voice_router._call_with_turn_id(legacy, "t1", "Hallo")
    voice_router._call_with_turn_id(modern, "t2", "Hallo")
    assert calls == [("legacy", "Hallo"), ("modern", "Hallo", "t2")]


def test_retry_gate_blocks_tool_started_by_parallel_retry(env):
    """Runde 2/P2: R1 und R2 übernehmen dieselbe Retry-Freigabe von L."""
    side = []
    r1_started = asyncio.Event()
    release_r1 = asyncio.Event()

    async def l_blocked(_kwargs):
        return "Geht nicht.", [{
            "tool": "set_reminder", "args": {},
            "result": "FEHLER: Tool 'set_reminder' ist für diesen Satz nicht freigegeben.",
        }]

    async def r1(kwargs):
        if kwargs["tool_gate"]("set_reminder") is None:
            side.append("r1")
        r1_started.set()
        await release_r1.wait()
        raise asyncio.CancelledError

    async def r2(kwargs):
        if kwargs["tool_gate"]("set_reminder") is None:
            side.append("r2")
        return "Okay.", []

    env.agent.behaviors.extend([l_blocked, r1, r2])
    _turn(env, "Erinnere mich um neun an Kreatin")

    async def run():
        env.clock.now += 5
        first = asyncio.create_task(env.handler.dashboard_respond(
            "Probier's noch mal", agent=env.agent, channel_name="voice",
        ))
        await asyncio.wait_for(r1_started.wait(), timeout=2)
        env.clock.now += 2
        await env.handler.dashboard_respond(
            "Probier's noch mal", agent=env.agent, channel_name="voice",
        )
        release_r1.set()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(run())
    assert "set_reminder" in set(env.agent.calls[2]["allowed_tools"])
    assert side == ["r1"]


def _speculative_turn(env, text, *, addressed):
    async def gate():
        return addressed

    async def run():
        try:
            return await env.handler.dashboard_respond(
                text, agent=env.agent, channel_name="voice", execution_gate=gate,
            )
        except asyncio.CancelledError:
            return "cancelled"

    return asyncio.run(run())


async def _respects_execution_gate(kwargs):
    if not await kwargs["execution_gate"]():
        raise asyncio.CancelledError
    return "Okay.", []


def test_rejected_cancel_segment_keeps_open_requests(env):
    """Runde 2/P3: „Lass das, Kevin!“ aus dem Fernseher darf nichts verwerfen."""
    env.agent.behaviors.extend([_cancelled_before_tools, _respects_execution_gate])
    assert _turn(env, FIRST) == "cancelled"
    env.clock.now += 2
    assert _speculative_turn(env, "Lass das, Kevin!", addressed=False) == "cancelled"
    # Auch das Widerrufssegment selbst übernimmt nichts.
    assert "set_reminder" not in _last_allowed(env)

    env.clock.now += 2
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert {"get_weather", "set_reminder"} <= _last_allowed(env)


def test_authorized_speculative_cancel_segment_clears_open_requests(env):
    env.agent.behaviors.extend([_cancelled_before_tools, _respects_execution_gate])
    assert _turn(env, FIRST) == "cancelled"
    env.clock.now += 2
    _speculative_turn(env, "Lass das, Kevin!", addressed=True)
    assert "set_reminder" not in _last_allowed(env)

    env.clock.now += 2
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "set_reminder" not in _last_allowed(env)


def test_tool_from_async_selection_of_current_sentence_passes_gate(env):
    """Runde 2/P3: Spotify kommt nur aus der Cloud-Auswahl von „lauter bitte“ allein."""
    gate_result = {}

    async def louder(kwargs):
        gate_result["spotify"] = kwargs["tool_gate"]("spotify")
        return "Lauter.", []

    async def select_async(text):
        env.selections.append(("async", text))
        if text == "lauter bitte":
            return ["spotify"], False
        return _fake_selection(text)

    env.monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    env.agent.behaviors.extend([_cancelled_before_tools, louder])
    assert _turn(env, FIRST) == "cancelled"

    env.clock.now += 5
    _turn(env, "lauter bitte")
    assert "spotify" in _last_allowed(env)
    assert gate_result["spotify"] is None
    assert ("async", "lauter bitte") in env.selections


def test_started_carried_tool_is_not_regranted_via_same_category_sentence(env):
    """Runde 3/P2: Kategorie-Auswahl des aktuellen Satzes enthält das schon gestartete Tool."""
    async def reminder_started_then_cancel(kwargs):
        assert kwargs["tool_gate"]("set_reminder") is None
        raise asyncio.CancelledError

    gate_result = {}

    async def calendar(kwargs):
        gate_result["set_reminder"] = kwargs["tool_gate"]("set_reminder")
        gate_result["get_calendar"] = kwargs["tool_gate"]("get_calendar")
        return "Morgen nichts.", []

    def productivity(text):
        names, force = _fake_selection(text)
        if "termine" in text.casefold():
            # Kategorie „productivity“: Termine, Kalender UND Erinnerungen.
            names = [*names, *(n for n in ("get_calendar", "set_reminder") if n not in names)]
        return names, force

    async def select_async(text):
        return productivity(text)

    env.monkeypatch.setattr(message_handler, "_keyword_tool_selection", productivity)
    env.monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    env.agent.behaviors.extend([reminder_started_then_cancel, calendar])
    assert _turn(env, "Erinnere mich morgen um neun an Kreatin") == "cancelled"

    env.clock.now += 5
    _turn(env, "und welche Termine habe ich morgen?")
    assert "set_reminder" not in _last_allowed(env)
    assert gate_result["set_reminder"] is not None
    assert "bereits" in gate_result["set_reminder"]
    assert gate_result["get_calendar"] is None


def test_turn_before_cancel_reported_later_does_not_become_open(env):
    """_valid(): vor „stopp“ gestartet, Unterbrechung erst danach gemeldet."""
    sentence = "Mach das Licht dunkler und spiel was auf Spotify"
    command = FastCommand(tool="lampe", args={}, label="lampe-dunkler")
    env.monkeypatch.setattr(
        message_handler.fast_commands, "match_voice",
        lambda text: command if text == sentence else None,
    )

    async def run():
        await env.handler.dashboard_respond(
            sentence, agent=env.agent, channel_name="voice",
            defer_finish=True, voice_turn_id="fp",
        )
        env.clock.now += 2
        await env.handler.dashboard_respond(
            "Stopp", agent=env.agent, channel_name="voice", voice_turn_id="stop",
        )

    asyncio.run(run())
    env.handler.record_interrupted_voice_turn("Lampe", turn_id="fp")
    assert env.handler._voice_ledger().open == []

    env.clock.now += 2
    _turn(env, "Überblick bitte nicht zu ausführlich")
    assert "spotify" not in _last_allowed(env)


def test_retry_does_not_regrant_executed_tool_via_current_sentence_selection(env):
    """Runde 3/P2: auch im Retry-Kontext keine Ausnahme für bereits gelaufene Tools."""
    gate_result = {}

    async def retry(kwargs):
        gate_result["set_reminder"] = kwargs["tool_gate"]("set_reminder")
        return "Okay.", []

    async def select_async(text):
        if "noch mal" in text:
            return ["set_reminder"], False
        return _fake_selection(text)

    env.monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_async)
    env.agent.behaviors.extend([_success("set_reminder"), retry])
    _turn(env, "Erinnere mich heute Abend um neun an Kreatin")

    env.clock.now += 10
    _turn(env, "Probier's noch mal")
    assert "set_reminder" not in _last_allowed(env)
    assert gate_result["set_reminder"] is not None
