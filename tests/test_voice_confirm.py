"""Bestätigungs-Flow im Voice-Kanal (Live-Befund 16.10., 15:22).

Ein Tool-Aufruf, der mangels Freigabe blockiert wurde, wird als ausstehende
Bestätigung gemerkt. Ein kurzes „ja“ im nächsten autorisierten Voice-Turn gibt
genau diese Tools frei; Widerruf oder ein anderer Satz verwirft sie.
"""
import asyncio

import pytest

from core import message_handler
from tests import test_voice_carryover as carry
from tests.test_voice_carryover import _last_allowed, _speculative_turn, _turn

# Fixture aus den Carry-over-Tests wiederverwenden (gleiche Fake-Umgebung).
env = carry.env

FOUR = (
    ("get_weather", {"ort": "Nürnberg"}),
    ("set_reminder", {"text": "Kreatin", "zeit": "21:00"}),
    ("lampe", {"aktion": "dunkler"}),
    ("spotify", {"action": "play", "query": "ruhig"}),
)


def _attempt(calls, results, *, respect_gate=False):
    """Fake-Agent: ruft Tools wie Agent.run auf (Freigabe → Gate, sonst Sperr-Callback)."""
    async def run(kwargs):
        if respect_gate and kwargs.get("execution_gate") is not None:
            if not await kwargs["execution_gate"]():
                raise asyncio.CancelledError
        trace = []
        allowed = set(kwargs["allowed_tools"] or [])
        for name, args in calls:
            if name not in allowed:
                callback = kwargs.get("not_permitted_cb")
                result = callback(name, args) if callback else None
                results.append(("blocked", name, result))
                trace.append({"tool": name, "args": args, "result": result or "FEHLER"})
                continue
            blocked = kwargs["tool_gate"](name, args)
            if blocked is not None:
                results.append(("gate", name, blocked))
                trace.append({"tool": name, "args": args, "result": blocked})
                continue
            results.append(("ran", name, args))
            trace.append({"tool": name, "args": args, "result": "ok"})
        return "Antwort.", trace
    return run


def _block_four(env, results):
    env.agent.behaviors.append(_attempt(FOUR, results))
    _turn(env, "Probier einfach alles nochmal")
    assert [item[0] for item in results] == ["blocked"] * 4


def test_live_flow_explicit_confirmation_grants_all_blocked_tools(env):
    results = []
    _block_four(env, results)
    message = results[0][2]
    assert message.startswith("FEHLER")
    assert "Bestätigung" in message
    assert "nicht ausgeführt" in message
    assert "nicht verfügbar" in message  # Anweisung, das NICHT zu behaupten

    env.clock.now += 10
    env.agent.behaviors.append(_attempt(FOUR, results))
    _turn(env, "Ich bestätige es ausdrücklich.")

    call = env.agent.calls[-1]
    assert {"get_weather", "set_reminder", "lampe", "spotify"} <= set(call["allowed_tools"])
    assert call["force_tools"] is False
    assert [item[1] for item in results[4:] if item[0] == "ran"] == [
        "get_weather", "set_reminder", "lampe", "spotify",
    ]
    # Gemerkte Aufrufe mit Argumenten als Kontext für das Modell.
    assert "set_reminder" in call["system"]
    assert "Kreatin" in call["system"]


def test_confirmation_after_ttl_grants_nothing(env):
    results = []
    _block_four(env, results)
    env.clock.now += 120
    _turn(env, "ja")
    assert not ({"set_reminder", "lampe", "spotify"} & _last_allowed(env))


@pytest.mark.parametrize(
    "authorization_type", [None, "window", "stop_word", "speculative", "unknown"],
)
def test_missing_or_ineligible_voice_authorization_cannot_consume_confirmation(
    env, authorization_type,
):
    results = []
    pending = message_handler._PendingConfirmation(
        calls=(("set_reminder", {"text": "Kreatin", "zeit": "21:00"}),),
        created_at=env.clock.now,
        since=env.clock.now - 1,
    )
    ledger = env.handler._voice_ledger()
    ledger.pending = pending
    env.agent.behaviors.append(_attempt((
        ("set_reminder", {"text": "Kreatin", "zeit": "21:00"}),
    ), results))

    kwargs = {}
    if authorization_type is not None:
        kwargs["authorization_type"] = authorization_type
    asyncio.run(env.handler.dashboard_respond(
        "Okay.", agent=env.agent, channel_name="voice", **kwargs,
    ))

    assert not any(item[0] == "ran" and item[1] == "set_reminder" for item in results)
    assert ledger.pending is pending


@pytest.mark.parametrize("authorization_type", ["wakeword", "addressed", "speaker_verified"])
def test_explicit_authorization_types_can_consume_pending_confirmation(
    env, authorization_type,
):
    results = []
    pending = message_handler._PendingConfirmation(
        calls=(("set_reminder", {"text": "Kreatin", "zeit": "21:00"}),),
        created_at=env.clock.now,
        since=env.clock.now - 1,
    )
    ledger = env.handler._voice_ledger()
    ledger.pending = pending
    env.agent.behaviors.append(_attempt((
        ("set_reminder", {"text": "Kreatin", "zeit": "21:00"}),
    ), results))

    asyncio.run(env.handler.dashboard_respond(
        "Okay.", agent=env.agent, channel_name="voice",
        authorization_type=authorization_type,
    ))

    assert any(item[0] == "ran" and item[1] == "set_reminder" for item in results)
    assert ledger.pending is None


@pytest.mark.parametrize("rejection", ["nein", "Stopp, lass es", "doch nicht"])
def test_rejection_clears_pending_confirmation(env, rejection):
    results = []
    _block_four(env, results)
    env.clock.now += 3
    _turn(env, rejection)
    env.clock.now += 3
    _turn(env, "ja")
    assert not ({"set_reminder", "lampe", "spotify"} & _last_allowed(env))


def test_other_sentence_clears_pending_confirmation(env):
    results = []
    _block_four(env, results)
    env.clock.now += 3
    _turn(env, "Wie spät ist es eigentlich")
    env.clock.now += 3
    _turn(env, "ja bitte")
    assert not ({"set_reminder", "lampe", "spotify"} & _last_allowed(env))


def test_unauthorized_confirmation_runs_nothing_and_keeps_pending(env):
    results = []
    _block_four(env, results)
    env.clock.now += 3
    env.agent.behaviors.append(_attempt(FOUR, results, respect_gate=True))
    assert _speculative_turn(env, "ja, mach das", addressed=False) == "cancelled"
    assert len(results) == 4

    env.clock.now += 3
    _turn(env, "ja, mach das")
    assert "set_reminder" in _last_allowed(env)


def test_confirmation_does_not_run_tool_twice_in_parallel(env):
    """Zwei spekulative „ja“: der erste startet lampe, der zweite darf sie nicht nochmal."""
    results = []
    env.agent.behaviors.append(_attempt((("lampe", {"aktion": "aus"}),), results))
    _turn(env, "Probier einfach alles nochmal")

    first_in_run, second_in_run = asyncio.Event(), asyncio.Event()
    started, release = asyncio.Event(), asyncio.Event()
    addressed_first, addressed_second = asyncio.Event(), asyncio.Event()
    gate = {}

    def try_lampe(kwargs):
        if "lampe" not in kwargs["allowed_tools"]:
            return "nicht freigegeben"
        return kwargs["tool_gate"]("lampe", {"aktion": "aus"})

    async def first(kwargs):
        first_in_run.set()
        assert await kwargs["execution_gate"]()
        gate["first"] = try_lampe(kwargs)
        started.set()
        await release.wait()
        return "Aus.", [{"tool": "lampe", "args": {}, "result": "ok"}]

    async def second(kwargs):
        gate["second_allowed"] = "lampe" in kwargs["allowed_tools"]
        second_in_run.set()
        assert await kwargs["execution_gate"]()
        gate["second"] = try_lampe(kwargs)
        return "Okay.", []

    env.agent.behaviors.extend([first, second])

    def gate_for(event):
        async def wait():
            await event.wait()
            return True
        return wait

    async def run():
        env.clock.now += 3
        t1 = asyncio.create_task(env.handler.dashboard_respond(
            "ja", agent=env.agent, channel_name="voice",
            execution_gate=gate_for(addressed_first),
            authorization_type="speculative",
        ))
        await asyncio.wait_for(first_in_run.wait(), timeout=2)
        env.clock.now += 1
        t2 = asyncio.create_task(env.handler.dashboard_respond(
            "ja mach", agent=env.agent, channel_name="voice",
            execution_gate=gate_for(addressed_second),
            authorization_type="speculative",
        ))
        await asyncio.wait_for(second_in_run.wait(), timeout=2)
        addressed_first.set()
        await asyncio.wait_for(started.wait(), timeout=2)
        addressed_second.set()
        await asyncio.wait_for(t2, timeout=2)
        release.set()
        await asyncio.wait_for(t1, timeout=2)

    asyncio.run(run())
    assert gate["first"] is None
    assert gate["second_allowed"] is True
    assert gate["second"] is not None


def test_okay_reflex_is_bypassed_while_confirmation_pending(env, monkeypatch):
    from core import fast_commands

    monkeypatch.setattr(message_handler.fast_commands, "match_voice", fast_commands.match_voice)
    results = []
    _block_four(env, results)
    env.clock.now += 3
    calls_before = len(env.agent.calls)
    _turn(env, "Okay")
    assert len(env.agent.calls) == calls_before + 1
    assert "lampe" in _last_allowed(env)


@pytest.mark.parametrize("text", [
    "ja", "Ja bitte.", "mach", "mach das", "mach mal", "okay mach", "genau",
    "bestätige", "Ich bestätige es ausdrücklich.", "ich bestätige", "passt",
    "klar", "los", "Mantis, ja bitte", "ja mach das, Mantis",
])
def test_confirmation_phrases(text):
    assert message_handler._is_voice_confirmation(text)


@pytest.mark.parametrize("text", [
    "", "nein", "ja okay, dann lass es", "mach das Licht an",
    "ja aber erst morgen um neun", "Wie spät ist es", "doch nicht",
])
def test_non_confirmation_phrases(text):
    assert not message_handler._is_voice_confirmation(text)


def test_voice_instructions_forbid_claims_without_tool_call():
    assert "Behaupte nie" in message_handler.VOICE_RESPONSE_INSTRUCTIONS


def test_claim_without_tool_call_is_logged(env, monkeypatch):
    events = []
    monkeypatch.setattr(
        message_handler.db, "log_event",
        lambda type_, summary, detail=None: events.append((type_, summary, detail)),
    )

    async def claims(_kwargs):
        return "Ich habe jetzt alle vier Aufgaben einzeln versucht.", []

    env.agent.behaviors.append(claims)
    reply, _trace = _turn(env, "Ich bestätige es ausdrücklich.")
    assert reply.startswith("Ich habe jetzt")  # nur loggen, nicht umschreiben
    assert [event[0] for event in events] == ["voice_claim_without_tool"]

    async def honest(_kwargs):
        return "Soll ich das jetzt machen?", []

    env.agent.behaviors.append(honest)
    _turn(env, "Was meinst du?")
    assert len(events) == 1


def _gate_for(event):
    async def wait():
        await event.wait()
        return True
    return wait


def test_confirmation_consumed_by_parallel_yes_blocks_second(env):
    """Zweites „ja“ hat die Bestätigung nur gesehen, das erste hat sie verbraucht."""
    results = []
    _block_four(env, results)
    second_in_run, first_authorized = asyncio.Event(), asyncio.Event()
    addressed_second = asyncio.Event()
    release_first = asyncio.Event()
    gate = {}

    async def first(kwargs):
        first_authorized.set()
        await release_first.wait()
        return "Okay.", []

    async def second(kwargs):
        gate["allowed"] = "spotify" in kwargs["allowed_tools"]
        second_in_run.set()
        assert await kwargs["execution_gate"]()
        gate["spotify"] = kwargs["tool_gate"]("spotify")
        return "Okay.", []

    env.agent.behaviors.extend([second, first])

    async def run():
        env.clock.now += 3
        t2 = asyncio.create_task(env.handler.dashboard_respond(
            "ja mach", agent=env.agent, channel_name="voice",
            execution_gate=_gate_for(addressed_second),
            authorization_type="speculative",
        ))
        await asyncio.wait_for(second_in_run.wait(), timeout=2)
        t1 = asyncio.create_task(env.handler.dashboard_respond(
            "ja", agent=env.agent, channel_name="voice",
            authorization_type="wakeword",
        ))
        await asyncio.wait_for(first_authorized.wait(), timeout=2)
        addressed_second.set()
        await asyncio.wait_for(t2, timeout=2)
        release_first.set()
        await asyncio.wait_for(t1, timeout=2)

    asyncio.run(run())
    assert gate["allowed"] is True
    assert gate["spotify"] is not None


def test_confirmed_tool_started_by_other_turn_meanwhile_is_blocked(env):
    """„ja“ autorisiert, danach startet ein anderer Turn lampe → „ja“ darf nicht nochmal."""
    results = []
    _block_four(env, results)
    ja_authorized, release_ja = asyncio.Event(), asyncio.Event()
    gate = {}

    async def ja(kwargs):
        ja_authorized.set()
        await release_ja.wait()
        gate["lampe"] = kwargs["tool_gate"]("lampe")
        return "Okay.", []

    async def light(kwargs):
        gate["other"] = kwargs["tool_gate"]("lampe")
        return "Licht aus.", [{"tool": "lampe", "args": {}, "result": "ok"}]

    env.agent.behaviors.extend([ja, light])

    async def run():
        env.clock.now += 3
        t1 = asyncio.create_task(env.handler.dashboard_respond(
            "ja", agent=env.agent, channel_name="voice",
            authorization_type="wakeword",
        ))
        await asyncio.wait_for(ja_authorized.wait(), timeout=2)
        env.clock.now += 1
        await env.handler.dashboard_respond(
            "Mach das Licht aus", agent=env.agent, channel_name="voice",
            authorization_type="wakeword",
        )
        release_ja.set()
        await asyncio.wait_for(t1, timeout=2)

    asyncio.run(run())
    assert gate["other"] is None
    assert gate["lampe"] is not None


def test_only_allowlisted_tools_become_confirmable(env):
    """Security 1: E-Mail-Versand o. ä. wird nie per „ja“ bestätigbar."""
    results = []
    calls = (("email_send", {"to": "x@example.org", "body": "Hi"}), ("lampe", {"aktion": "aus"}))
    env.agent.behaviors.append(_attempt(calls, results))
    _turn(env, "Probier einfach alles nochmal")
    email_message = next(item[2] for item in results if item[1] == "email_send")
    assert "Bestätigung" not in (email_message or "")
    pending = env.handler._voice_ledger().pending
    assert pending is not None and pending.names == frozenset({"lampe"})

    env.clock.now += 3
    _turn(env, "ja")
    assert "lampe" in _last_allowed(env)
    assert "email_send" not in _last_allowed(env)


def test_non_allowlisted_pending_is_neither_granted_nor_passed_by_gate(env):
    """Security 1: auch eine (fremd) gemerkte Bestätigung wird an der Allowlist gefiltert."""
    gate = {}

    async def confirm(kwargs):
        gate["email_send"] = kwargs["tool_gate"]("email_send", {"to": "x"})
        return "Okay.", []

    ledger = env.handler._voice_ledger()
    ledger.pending = message_handler._PendingConfirmation(
        calls=(("email_send", {"to": "x"}),), created_at=env.clock.now,
        since=env.clock.now - 1,
    )
    env.agent.behaviors.append(confirm)
    env.clock.now += 2
    _turn(env, "ja")
    assert "email_send" not in _last_allowed(env)
    assert gate["email_send"] is not None


def test_confirmed_tool_requires_same_arguments(env):
    """Security 2: Bestätigung gilt nur für die gemerkten Argumente."""
    results = []
    _block_four(env, results)
    gate = {}

    async def confirm(kwargs):
        gate_fn = kwargs["tool_gate"]
        gate["other"] = gate_fn("set_reminder", {"text": "Bier", "zeit": "21:00"})
        gate["same"] = gate_fn("set_reminder", {"zeit": "21:00", "text": " Kreatin "})
        return "Okay.", []

    env.agent.behaviors.append(confirm)
    env.clock.now += 3
    _turn(env, "ja")
    assert gate["other"] is not None and "weichen" in gate["other"]
    assert gate["same"] is None


def test_async_own_selection_cannot_change_confirmed_arguments(env, monkeypatch):
    results = []
    _block_four(env, results)

    async def select_tools(text):
        if text == "ja":
            return ["set_reminder"], False
        return [], False

    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", select_tools)
    env.agent.behaviors.append(_attempt((
        ("set_reminder", {"text": "Bier", "zeit": "23:00"}),
    ), results))
    env.clock.now += 3
    _turn(env, "ja")

    assert not any(item[0] == "ran" and item[1] == "set_reminder" for item in results)
    assert any(item[0] == "gate" and "weichen" in item[2] for item in results)


def test_retry_keeps_confirmed_arguments_after_mismatch(env):
    results = []
    _block_four(env, results)
    changed_args = {"text": "Bier", "zeit": "23:00"}
    env.agent.behaviors.append(_attempt((("set_reminder", changed_args),), results))
    env.clock.now += 3
    _turn(env, "ja")
    assert any(item[0] == "gate" and "weichen" in item[2] for item in results)

    env.clock.now += 3
    env.agent.behaviors.append(_attempt((("set_reminder", changed_args),), results))
    _turn(env, "Nochmal")

    assert not any(item[0] == "ran" and item[1] == "set_reminder" for item in results)
    assert any(item[0] == "gate" and "weichen" in item[2] for item in results[5:])


def test_retry_confirmation_arguments_survive_three_consecutive_retries(env):
    results = []
    _block_four(env, results)
    changed_args = {"text": "Bier", "zeit": "23:00"}
    env.agent.behaviors.append(_attempt((("set_reminder", changed_args),), results))
    env.clock.now += 3
    _turn(env, "ja")

    for _ in range(3):
        env.clock.now += 3
        env.agent.behaviors.append(_attempt((("set_reminder", changed_args),), results))
        _turn(env, "Nochmal")

    assert not any(item[0] == "ran" and item[1] == "set_reminder" for item in results)
    assert sum(
        item[0] == "gate" and "weichen" in item[2]
        for item in results
    ) == 4


def test_real_agent_retry_binding_survives_three_consecutive_retries(env, monkeypatch):
    from core.agent import Agent

    results = []
    _block_four(env, results)
    changed_args = {"text": "Bier", "zeit": "23:00"}
    executions = []

    async def execute(tool, args):
        executions.append((tool, args))
        return "Erinnerung gespeichert."

    monkeypatch.setattr(message_handler.skills.T, "execute", execute)

    def tool_call():
        return {"function": {
            "name": "set_reminder",
            "arguments": changed_args,
        }}

    class Backend:
        model_name = "fake"
        sends_full_tool_catalog = False

        def __init__(self):
            self.responses = []
            for _ in range(4):
                self.responses.extend([("", [tool_call()]), ("Antwort.", [])])

        async def call(self, *_args, **_kwargs):
            return self.responses.pop(0)

    agent = Agent(Backend())

    async def turn(text):
        return await env.handler.dashboard_respond(
            text, agent=agent, channel_name="voice", authorization_type="wakeword",
        )

    async def run():
        await turn("ja")
        for _ in range(3):
            env.clock.now += 3
            await turn("Nochmal")

    asyncio.run(run())

    assert executions == []


def test_real_agent_consumes_confirmed_call_once_per_turn(env, monkeypatch):
    from core.agent import Agent

    results = []
    _block_four(env, results)
    executions = []

    async def execute(tool, args):
        executions.append((tool, args))
        return "Erinnerung gespeichert."

    monkeypatch.setattr(message_handler.skills.T, "execute", execute)

    def tool_call():
        return {"function": {
            "name": "set_reminder",
            "arguments": {"text": "Kreatin", "zeit": "21:00"},
        }}

    class Backend:
        model_name = "fake"

        def __init__(self):
            self.responses = [("", [tool_call(), tool_call()]), ("Fertig.", [])]

        async def call(self, *_args, **_kwargs):
            return self.responses.pop(0)

    async def confirm():
        return await env.handler.dashboard_respond(
            "ja", agent=Agent(Backend()), channel_name="voice",
            authorization_type="wakeword",
        )

    _reply, trace = asyncio.run(confirm())

    assert executions == [(
        "set_reminder", {"text": "Kreatin", "zeit": "21:00"},
    )], trace


def test_confirmation_keeps_case_sensitive_string_arguments(env):
    results = []
    env.agent.behaviors.append(_attempt((
        ("complete_task", {"query": "Maße prüfen"}),
    ), results))
    _turn(env, "Probier einfach alles nochmal")
    assert results[0][0] == "blocked"

    env.agent.behaviors.append(_attempt((
        ("complete_task", {"query": "Masse prüfen"}),
    ), results))
    env.clock.now += 3
    _turn(env, "ja")

    assert not any(item[0] == "ran" and item[1] == "complete_task" for item in results)
    assert any(item[0] == "gate" and "weichen" in item[2] for item in results)


def test_older_turn_finishing_late_does_not_set_pending(env):
    """Security 3: A blockiert, B wird inzwischen autorisiert, A endet → kein pending."""
    results = []
    release_a, a_running = asyncio.Event(), asyncio.Event()
    blocked_a = _attempt((("lampe", {"aktion": "aus"}),), results)

    async def slow_a(kwargs):
        reply = await blocked_a(kwargs)
        a_running.set()
        await release_a.wait()
        return reply

    env.agent.behaviors.extend([slow_a])

    async def run():
        task = asyncio.create_task(env.handler.dashboard_respond(
            "Probier einfach alles nochmal", agent=env.agent, channel_name="voice",
            authorization_type="wakeword",
        ))
        await asyncio.wait_for(a_running.wait(), timeout=2)
        env.clock.now += 2
        await env.handler.dashboard_respond(
            "Wie spät ist es", agent=env.agent, channel_name="voice",
            authorization_type="wakeword",
        )
        release_a.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    assert env.handler._voice_ledger().pending is None
    env.clock.now += 2
    _turn(env, "ja")
    assert "lampe" not in _last_allowed(env)
