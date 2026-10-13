"""Der Gym-Fast-Path liegt vor KZG, Agent und Post-Turn-Learning."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from communication.base import IncomingMessage
from core import db
from core.message_handler import MessageHandler
from domains.gym import coach as coach_module
from domains.gym import session as gym_session
from domains.gym.aliases import builtin_aliases


def _handler(monkeypatch, *, agent_reply=("Normale Agentenantwort", [])):
    persisted = []
    kzg = SimpleNamespace(
        added=[], add=lambda role, text: kzg.added.append((role, text)),
        get_recent_turns=lambda n: [], recent_messages=lambda **_kwargs: [],
    )
    lzg = SimpleNamespace(saved=[], save_kzg_turn=lambda role, text: lzg.saved.append((role, text)))
    agent = SimpleNamespace(model_name="test-chain", run=AsyncMock(return_value=agent_reply))
    channel = SimpleNamespace(
        send=AsyncMock(), send_typing=AsyncMock(), supports_streaming=False,
    )
    handler = MessageHandler(
        kzg=kzg, lzg=lzg, agent=agent,
        prompt_builder=SimpleNamespace(build=AsyncMock(return_value="normal system"), _last_mem_ids=[]),
        channel=channel,
        proactive_tracker=SimpleNamespace(record_user_response=Mock()),
        forgetting=SimpleNamespace(bump_recall=Mock()),
        extractor=SimpleNamespace(extract_from_exchange=AsyncMock(return_value=0)),
        bg_llm=SimpleNamespace(chat=AsyncMock()),
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=Mock(),
    )
    handler.gym_coach._latest_pending = lambda: None
    monkeypatch.setattr(coach_module, "load_aliases", builtin_aliases)
    monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        "core.message_handler._safe_task",
        lambda coro, _label: coro.close() if hasattr(coro, "close") else None,
    )
    monkeypatch.setattr("core.message_handler.ui_state.maybe_update_ui", lambda *_args: None)
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gym_session, "open_session", lambda *_args, **_kwargs: None)

    def execute(sql, params=()):
        if "INSERT INTO chat_messages" in sql:
            persisted.append((sql, params))
    monkeypatch.setattr(db, "execute", execute)
    return handler, persisted, kzg, lzg, agent, channel


def test_clear_set_uses_no_agent_and_never_enters_memory(monkeypatch):
    handler, persisted, kzg, lzg, agent, channel = _handler(monkeypatch)
    monkeypatch.setattr(gym_session, "add_sets", lambda parsed, **_kwargs: {
        "exercise": parsed.exercise,
        "sets": [{"weight_kg": 80, "reps": 8}, {"weight_kg": 80, "reps": 7}],
        "previous_sets": [],
        "progression": {"weight_kg": 80, "target_reps": [8, 8]},
        "records": {},
    })
    message = IncomingMessage("Bank 80 8 7", "timo", datetime(2026, 10, 14))

    asyncio.run(handler.handle(message, None))

    assert not agent.run.await_args_list
    assert not kzg.added
    assert not lzg.saved
    assert len(persisted) == 2
    assert [params[0] for _sql, params in persisted] == ["user", "assistant"]
    assert all('"use_case": "gym"' in params[3] for _sql, params in persisted)
    assert '"gym_pending": {"resolved": true}' in persisted[1][1][3]
    assert channel.send.await_args.args[0].startswith("✅ Bankdrücken")


def test_non_gym_message_keeps_the_normal_agent_path_and_metadata(monkeypatch):
    handler, persisted, kzg, lzg, agent, channel = _handler(monkeypatch)
    message = IncomingMessage("Was mach ich heute?", "timo", datetime(2026, 10, 14))

    asyncio.run(handler.handle(message, None))

    assert agent.run.await_count == 1
    assert kzg.added[0] == ("user", "Was mach ich heute?")
    assert lzg.saved[0] == ("user", "Was mach ich heute?")
    assert all('"use_case"' not in params[3] for _sql, params in persisted)
    assert channel.send.await_args.args[0] == "Normale Agentenantwort"


def test_smalltalk_numbers_are_not_misrouted_to_gym(monkeypatch):
    handler, persisted, _kzg, _lzg, agent, _channel = _handler(monkeypatch)

    asyncio.run(handler.handle(
        IncomingMessage("Heute 80 8 8", "timo", datetime(2026, 10, 14)), None
    ))

    assert agent.run.await_count == 1
    assert all('"use_case"' not in params[3] for _sql, params in persisted)


def test_parser_fallback_sends_only_current_text_and_aliases_to_chat_chain(monkeypatch):
    raw = '{"exercise":"Cable Fly","sets":[{"weight_kg":12.5,"reps":12},{"weight_kg":12.5,"reps":10}]}'
    handler, persisted, _kzg, _lzg, agent, _channel = _handler(monkeypatch, agent_reply=(raw, []))
    monkeypatch.setattr(gym_session, "add_sets", Mock(side_effect=AssertionError("must wait for confirmation")))
    message = IncomingMessage(
        "Neue Übung 80 kg, 8 Wiederholungen, 8 Wiederholungen", "timo", datetime(2026, 10, 14)
    )

    asyncio.run(handler.handle(message, None))

    agent.run.assert_awaited_once()
    call = agent.run.await_args.kwargs
    assert call["messages"] == [{"role": "user", "content": message.text}]
    assert call["use_tools"] is False
    assert call["allowed_tools"] == []
    assert "bench press" in call["system"].lower()
    assert len(persisted) == 2
    assistant_meta = persisted[1][1][3]
    assert '"gym_pending"' in assistant_meta


def test_discord_gym_reply_uses_the_incoming_dm(monkeypatch):
    handler, persisted, _kzg, _lzg, agent, telegram = _handler(monkeypatch)
    destination = SimpleNamespace(send=AsyncMock())
    handler.channels = {"discord": SimpleNamespace(for_message=lambda _raw: destination)}
    monkeypatch.setattr(gym_session, "add_sets", lambda parsed, **_kwargs: {
        "exercise": parsed.exercise,
        "sets": [{"weight_kg": 80, "reps": 8}],
        "previous_sets": [],
        "progression": {"weight_kg": 80, "target_reps": [8, 8, 8]},
        "records": {},
    })
    message = IncomingMessage(
        "Bank 80 8", "timo", datetime(2026, 10, 14),
        raw=object(), channel="discord",
    )

    asyncio.run(handler.handle(message, None))

    destination.send.assert_awaited_once()
    telegram.send.assert_not_awaited()
    agent.run.assert_not_awaited()
    assert all(params[2] == "discord" for _sql, params in persisted)

