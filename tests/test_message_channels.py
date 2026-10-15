import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from communication.base import IncomingMessage
from communication.discord import DiscordChannel
from core import db, log_observability
from core.idle_loop import IdleLoop
from core.message_handler import (
    MessageHandler,
    VOICE_RESPONSE_INSTRUCTIONS,
    _messages_with_voice_reminder,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orchestrator import Orchestrator
from web.routers import chat, voice
from core.prompt_builder import CacheableSystemPrompt


def _make_orchestrator(monkeypatch):
    inserted = []

    class KZG:
        def add(self, *_args):
            return None

        def get_recent_turns(self, n):
            return []

        def recent_messages(self, max_tokens):
            return []

    idle_loop = object.__new__(IdleLoop)
    idle_loop._state = "idle"
    idle_loop._task = None

    async def wait_until_cancelled():
        await asyncio.Event().wait()

    idle_loop._loop = wait_until_cancelled
    channel = SimpleNamespace(
        supports_streaming=False,
        send=AsyncMock(),
        send_typing=AsyncMock(),
    )
    agent = SimpleNamespace(
        model_name="channel-test-model",
        run=AsyncMock(return_value=("Antwort aus dem Agenten", [])),
    )
    handler = MessageHandler(
        kzg=KZG(),
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        agent=agent,
        prompt_builder=SimpleNamespace(
            build=AsyncMock(return_value="system prompt"),
            _last_mem_ids=[],
        ),
        channel=channel,
        proactive_tracker=SimpleNamespace(record_user_response=lambda: None),
        forgetting=SimpleNamespace(bump_recall=lambda *_args: None),
        extractor=SimpleNamespace(extract_from_exchange=AsyncMock(return_value=0)),
        bg_llm=None,
        alphaprogression=SimpleNamespace(extract_link=lambda _text: None),
        on_user_active=idle_loop.pause,
    )

    def execute(sql, params=()):
        if "INSERT INTO chat_messages" in sql:
            inserted.append((sql, params))

    def discard_background(coro, _label):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", AsyncMock(return_value=([], [])))
    monkeypatch.setattr("core.message_handler.ui_state.maybe_update_ui", lambda *_args: None)
    monkeypatch.setattr("core.message_handler._safe_task", discard_background)
    monkeypatch.setattr("domains.accountability.intercept", lambda _text: None)

    orchestrator = object.__new__(Orchestrator)
    orchestrator.msg_handler = handler
    orchestrator.voice_agent = agent
    orchestrator.idle_loop = idle_loop
    return orchestrator, inserted, idle_loop


def _persisted_role_channels(inserted):
    return [(params[0], params[2]) for _sql, params in inserted]


@pytest.mark.parametrize(
    ("facade", "expected_max_tokens", "voice_instructions"),
    [("dashboard", 4000, False), ("voice", 241, True)],
)
def test_voice_turn_uses_spoken_prompt_and_configured_token_budget(
    monkeypatch, facade, expected_max_tokens, voice_instructions,
):
    orchestrator, _inserted, _idle_loop = _make_orchestrator(monkeypatch)
    monkeypatch.setattr("core.message_handler.config.VOICE_MAX_TOKENS", 241, raising=False)

    if facade == "voice":
        asyncio.run(orchestrator.voice_respond("Wie war mein Schlaf?"))
    else:
        asyncio.run(orchestrator.dashboard_respond("Wie war mein Schlaf?"))

    call = orchestrator.voice_agent.run.await_args
    assert call.kwargs["max_tokens"] == expected_max_tokens
    has_voice_instructions = "Deine Antworten werden vorgelesen" in call.kwargs["system"]
    assert has_voice_instructions is voice_instructions
    if voice_instructions:
        assert call.kwargs["system"].endswith(VOICE_RESPONSE_INSTRUCTIONS)


def test_voice_reminder_is_temporary_and_keeps_prompt_cache_prefix(monkeypatch):
    orchestrator, inserted, _idle_loop = _make_orchestrator(monkeypatch)
    handler = orchestrator.msg_handler

    class KZG:
        def __init__(self):
            self.turns = []

        def add(self, role, content):
            self.turns.append(SimpleNamespace(role=role, content=content))

        def recent_messages(self, max_tokens):
            return [SimpleNamespace(role=turn.role, content=turn.content) for turn in self.turns]

    kzg = KZG()
    handler.kzg = kzg
    handler.lzg.save_kzg_turn = lambda *args, **kwargs: None
    handler.prompt_builder.build = AsyncMock(return_value=CacheableSystemPrompt(
        "stable prefix\n\ndynamic context", cache_prefix="stable prefix",
    ))

    asyncio.run(orchestrator.voice_respond("Wie war mein Schlaf?"))

    call = orchestrator.voice_agent.run.await_args
    current_user_message = call.kwargs["messages"][-1]
    assert "Wie war mein Schlaf?" in current_user_message["content"]
    assert "[Sprachmodus:" in current_user_message["content"]
    assert current_user_message["content"].endswith(
        "[Sprachmodus: höchstens drei kurze gesprochene Sätze, keine Aufzählungen; "
        "alle Zahlen, Daten, Uhrzeiten, Dezimalzahlen und Einheiten in Wörtern]"
    )
    assert kzg.turns[0].content == "Wie war mein Schlaf?"
    user_row = next(params for _sql, params in inserted if params[0] == "user")
    assert user_row[1] == "Wie war mein Schlaf?"
    assert call.kwargs["system"].endswith(VOICE_RESPONSE_INSTRUCTIONS)
    assert call.kwargs["system"].cache_prefix == "stable prefix"


def test_voice_time_reflex_skips_llm_and_persists_as_reflex_turn(monkeypatch):
    orchestrator, inserted, _idle_loop = _make_orchestrator(monkeypatch)
    handler = orchestrator.msg_handler
    handler.lzg.save_kzg_turn = lambda *args, **kwargs: None

    response, trace = asyncio.run(orchestrator.voice_respond("Wie spät ist es?"))

    assert response.startswith("Es ist ")
    assert trace and trace[0]["reflex"] is True
    orchestrator.voice_agent.run.assert_not_awaited()
    handler.extractor.extract_from_exchange.assert_not_awaited()
    assert _persisted_role_channels(inserted) == [("user", "voice"), ("assistant", "voice")]
    assistant = next(params for _sql, params in inserted if params[0] == "assistant")
    assert '"reflex": true' in assistant[3]


@pytest.mark.parametrize("text", ["Wie spät ist es in Tokio?", "Wie wird mein Tag?"])
def test_nonlocal_voice_questions_still_go_to_llm(monkeypatch, text):
    orchestrator, _inserted, _idle_loop = _make_orchestrator(monkeypatch)

    response, trace = asyncio.run(orchestrator.voice_respond(text))

    assert response == "Antwort aus dem Agenten"
    assert trace == []
    orchestrator.voice_agent.run.assert_awaited_once()


def test_prompt_history_filters_empty_and_whitespace_turns():
    from memory.chat_messages import filter_prompt_messages

    messages = filter_prompt_messages([
        {"role": "user", "content": "  \n"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "valid"},
    ])

    assert messages == [{"role": "user", "content": "valid"}]


def test_empty_message_is_not_persisted(monkeypatch):
    orchestrator, inserted, _ = _make_orchestrator(monkeypatch)
    handler = orchestrator.msg_handler
    lzg_saved = []
    handler.lzg.save_kzg_turn = lambda *args, **kwargs: lzg_saved.append((args, kwargs))

    handler._persist_msg("user", " \t\n")

    assert inserted == []
    assert lzg_saved == []


def test_incoming_message_defaults_to_telegram_channel():
    message = IncomingMessage("hi", "1", datetime(2026, 10, 13))

    assert message.channel == "telegram"


def test_telegram_handler_persists_both_roles_with_telegram_channel(monkeypatch):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    message = IncomingMessage("hello", "1", datetime(2026, 10, 13), channel="telegram")

    asyncio.run(orchestrator.handle_message(message))

    assert _persisted_role_channels(inserted) == [
        ("user", "telegram"),
        ("assistant", "telegram"),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_discord_message_uses_its_reply_channel_and_persists_discord(monkeypatch):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    telegram = orchestrator.msg_handler.channel
    destination = SimpleNamespace(send=AsyncMock())
    discord = DiscordChannel(enabled=False, allowed_user_ids=("123",))
    orchestrator.msg_handler.channels = {"discord": discord}
    message = IncomingMessage(
        "hello from Discord", "123", datetime(2026, 10, 13),
        raw=SimpleNamespace(channel=destination), channel="discord",
    )

    asyncio.run(orchestrator.handle_message(message))

    assert destination.send.await_args.args == ("Antwort aus dem Agenten",)
    telegram.send.assert_not_awaited()
    assert _persisted_role_channels(inserted) == [
        ("user", "discord"),
        ("assistant", "discord"),
    ]
    assert idle_loop._state == "idle"


def test_failed_discord_send_still_persists_answer_and_tool_metadata(monkeypatch, caplog):
    orchestrator, inserted, _idle_loop = _make_orchestrator(monkeypatch)
    destination = SimpleNamespace(
        send=AsyncMock(side_effect=RuntimeError("discord-secret transport details"))
    )
    orchestrator.msg_handler.channels = {
        "discord": DiscordChannel(enabled=False, allowed_user_ids=("123",)),
    }
    orchestrator.msg_handler.agent.run = AsyncMock(
        return_value=("Antwort trotz Transportfehler", [{"tool": "get_health"}])
    )
    message = IncomingMessage(
        "Prüffrage", "123", datetime(2026, 10, 13),
        raw=SimpleNamespace(channel=destination), channel="discord",
    )

    with caplog.at_level(logging.WARNING, logger="core.message_handler"):
        asyncio.run(orchestrator.handle_message(message))

    assert _persisted_role_channels(inserted) == [
        ("user", "discord"),
        ("assistant", "discord"),
    ]
    assistant_sql, assistant_params = inserted[-1]
    assert assistant_params[1] == "Antwort trotz Transportfehler"
    assert '"tools": ["get_health"]' in assistant_params[3]
    assert "RuntimeError" in caplog.text
    assert "discord-secret" not in caplog.text


def test_discord_alpha_progression_response_goes_to_the_incoming_dm(monkeypatch):
    orchestrator, _inserted, _idle_loop = _make_orchestrator(monkeypatch)
    destination = SimpleNamespace(send=AsyncMock())
    orchestrator.msg_handler.channels = {
        "discord": DiscordChannel(enabled=False, allowed_user_ids=("123",)),
    }
    orchestrator.msg_handler.alphaprogression.extract_link = lambda _text: "https://example.test/workout"
    orchestrator.msg_handler.alphaprogression.fetch_and_log = lambda _url: "Training importiert"
    message = IncomingMessage(
        "https://example.test/workout", "123", datetime(2026, 10, 13),
        raw=SimpleNamespace(channel=destination), channel="discord",
    )

    asyncio.run(orchestrator.handle_message(message))

    assert destination.send.await_args.args == ("Training importiert",)


def test_discord_clarification_response_goes_to_the_incoming_dm(monkeypatch):
    orchestrator, _inserted, _idle_loop = _make_orchestrator(monkeypatch)
    destination = SimpleNamespace(send=AsyncMock())
    orchestrator.msg_handler.channels = {
        "discord": DiscordChannel(enabled=False, allowed_user_ids=("123",)),
    }
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [{"id": 17}])
    message = IncomingMessage(
        "Ja, bitte weitermachen", "123", datetime(2026, 10, 13),
        raw=SimpleNamespace(channel=destination), channel="discord",
    )

    asyncio.run(orchestrator.handle_message(message))

    assert destination.send.await_args.args == (
        "Danke! Ich mache mit deiner Antwort weiter an der Aufgabe. 👍",
    )


def test_cancelled_discord_turn_stops_typing_and_resumes_idle_loop(monkeypatch):
    orchestrator, _inserted, idle_loop = _make_orchestrator(monkeypatch)
    typing_count = [0]
    started = asyncio.Event()

    class Typing:
        async def __aenter__(self):
            typing_count[0] += 1

        async def __aexit__(self, *_args):
            typing_count[0] -= 1

    destination = SimpleNamespace(
        send=AsyncMock(),
        typing=lambda: Typing(),
    )
    orchestrator.msg_handler.channels = {
        "discord": DiscordChannel(enabled=False, allowed_user_ids=("123",)),
    }

    async def wait_for_completion(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    orchestrator.msg_handler.agent.run = AsyncMock(side_effect=wait_for_completion)
    message = IncomingMessage(
        "hello", "123", datetime(2026, 10, 13),
        raw=SimpleNamespace(channel=destination), channel="discord",
    )

    async def run():
        task = asyncio.create_task(orchestrator.handle_message(message))
        await started.wait()
        assert typing_count == [1]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert typing_count == [0]
        assert idle_loop._state == "idle"

    asyncio.run(run())


def test_orchestrator_wires_discord_for_messages_and_reminder_dms(monkeypatch):
    import orchestrator as orchestrator_module

    class Capture:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    for name in (
        "KZG", "KnowledgeGraph", "Consolidator", "ForgettingCurve", "MemoryExtractor",
        "Reflection", "Agent", "WebSearch", "DashboardReader", "ReminderStore",
        "ProactiveTracker", "ProactiveEngine", "PromptBuilder", "IdleLoop",
    ):
        monkeypatch.setattr(orchestrator_module, name, Capture)

    autopilot = Capture
    monkeypatch.setattr(orchestrator_module, "Autopilot", autopilot)
    message_handler = Capture
    monkeypatch.setattr(orchestrator_module, "MessageHandler", message_handler)
    monkeypatch.setattr(orchestrator_module.config, "NOTIFY_CHANNEL", "discord")

    primary = object()
    route = object()
    discord = SimpleNamespace(for_first_allowed_user=lambda: route)
    orchestrator = Orchestrator(
        channel=primary,
        lzg=object(),
        thermal=object(),
        chat_llm=object(),
        bg_llm=object(),
        embed_llm=object(),
        agent_backend=object(),
        additional_channels={"discord": discord},
    )

    assert orchestrator.autopilot.kwargs["reminder_channel"] is route
    assert orchestrator.msg_handler.kwargs["channels"] == {
        "telegram": primary,
        "discord": discord,
    }


def test_orchestrator_starts_and_stops_additional_channels(monkeypatch):
    primary = SimpleNamespace(
        on_message_start=Mock(),
        on_message=Mock(),
        start=AsyncMock(),
    )
    discord = SimpleNamespace(
        on_message_start=Mock(),
        on_message=Mock(),
        start=AsyncMock(),
        stop=AsyncMock(),
    )
    idle_loop = SimpleNamespace(
        resume=Mock(),
        pause=Mock(),
        monitoring_loop=AsyncMock(),
    )
    orchestrator = object.__new__(Orchestrator)
    orchestrator.channel = primary
    orchestrator.additional_channels = {"discord": discord}
    orchestrator.idle_loop = idle_loop
    orchestrator.lzg = SimpleNamespace(close=Mock())
    orchestrator._dashboard = SimpleNamespace(close=Mock())
    orchestrator._register_services = Mock()
    orchestrator._init_db = AsyncMock()
    orchestrator._init_skills = AsyncMock()
    orchestrator._restore_memory = AsyncMock()
    orchestrator._warmup = AsyncMock()
    monkeypatch.setattr("orchestrator.log_background_job_status", lambda: None)
    monkeypatch.setattr("orchestrator.log_event", lambda *_args: None)
    monkeypatch.setattr("core.log_observability.install", lambda: None)
    monkeypatch.setattr(db, "close_pool", lambda: None)

    async def run():
        await orchestrator.start()
        await orchestrator.stop()

    asyncio.run(run())

    primary.on_message_start.assert_called_once_with(orchestrator.begin_incoming_message)
    primary.on_message.assert_called_once_with(orchestrator.handle_message)
    discord.on_message_start.assert_called_once_with(orchestrator.begin_incoming_message)
    discord.on_message.assert_called_once_with(orchestrator.handle_message)
    primary.start.assert_awaited_once()
    discord.start.assert_awaited_once()
    discord.stop.assert_awaited_once()


@pytest.mark.parametrize("channel_name", ["web", "ios", "mcp"])
def test_dashboard_routes_persist_both_roles_with_ingress_channel(monkeypatch, channel_name):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    app = FastAPI()
    app.include_router(chat.build_router(orchestrator))

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"text": "hello", "channel": channel_name},
        )

    assert response.status_code == 200
    assert response.json()["response"] == "Antwort aus dem Agenten"
    assert _persisted_role_channels(inserted) == [
        ("user", channel_name),
        ("assistant", channel_name),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_voice_route_persists_both_roles_with_voice_channel(monkeypatch):
    orchestrator, inserted, idle_loop = _make_orchestrator(monkeypatch)
    monkeypatch.setattr(voice, "transcribe_audio", AsyncMock(return_value="Wie wird mein Tag?"))
    monkeypatch.setattr(voice, "is_addressed_to_mantis", AsyncMock(return_value=True))
    monkeypatch.setattr(voice, "synthesize", AsyncMock(return_value=b"audio"))
    app = FastAPI()
    app.include_router(voice.build_router(orchestrator))

    with TestClient(app) as client:
        response = client.post(
            "/api/voice/segment",
            files={"audio": ("segment.wav", b"audio input", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "Antwort aus dem Agenten"
    assert _persisted_role_channels(inserted) == [
        ("user", "voice"),
        ("assistant", "voice"),
    ]
    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_overlapping_dashboard_and_voice_turns_keep_idle_loop_paused_until_last_finishes(monkeypatch):
    orchestrator, _inserted, idle_loop = _make_orchestrator(monkeypatch)

    async def run_turns():
        started = {channel: asyncio.Event() for channel in ("web", "voice")}
        finish = {channel: asyncio.Event() for channel in ("web", "voice")}

        async def wait_for_finish(_text, channel_name, **_kwargs):
            started[channel_name].set()
            await finish[channel_name].wait()
            return "Antwort", []

        orchestrator.msg_handler._agent_turn = wait_for_finish
        dashboard_task = asyncio.create_task(orchestrator.dashboard_respond("dashboard"))
        await started["web"].wait()
        voice_task = asyncio.create_task(orchestrator.voice_respond("voice"))
        await started["voice"].wait()

        finish["web"].set()
        await dashboard_task
        assert idle_loop._state == "conversation"

        finish["voice"].set()
        await voice_task

    asyncio.run(run_turns())
    assert idle_loop._state == "idle"


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_overlapping_turn_failure_or_cancellation_keeps_idle_loop_paused(monkeypatch, failure):
    orchestrator, _inserted, idle_loop = _make_orchestrator(monkeypatch)

    async def run_turns():
        dashboard_started = asyncio.Event()
        finish_dashboard = asyncio.Event()
        voice_started = asyncio.Event()
        finish_voice = asyncio.Event()

        async def dashboard_fails_or_voice_waits(_text, channel_name, **_kwargs):
            if channel_name == "web":
                dashboard_started.set()
                await finish_dashboard.wait()
                raise failure("turn aborted")
            voice_started.set()
            await finish_voice.wait()
            return "Antwort", []

        orchestrator.msg_handler._agent_turn = dashboard_fails_or_voice_waits
        dashboard_task = asyncio.create_task(orchestrator.dashboard_respond("dashboard"))
        await dashboard_started.wait()
        voice_task = asyncio.create_task(orchestrator.voice_respond("voice"))
        await voice_started.wait()

        finish_dashboard.set()
        with pytest.raises(failure, match="turn aborted"):
            await dashboard_task
        assert idle_loop._state == "conversation"

        finish_voice.set()
        await voice_task

    asyncio.run(run_turns())
    assert idle_loop._state == "idle"


@pytest.mark.parametrize("facade", ["dashboard", "voice", "telegram"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_turn_failure_or_cancellation_always_resumes_idle_loop(monkeypatch, facade, failure):
    orchestrator, _inserted, idle_loop = _make_orchestrator(monkeypatch)

    async def fail_turn(*_args, **_kwargs):
        raise failure("turn aborted")

    orchestrator.msg_handler._agent_turn = fail_turn

    async def run_turn():
        if facade == "dashboard":
            await orchestrator.dashboard_respond("hello")
        elif facade == "voice":
            await orchestrator.voice_respond("hello")
        else:
            message = IncomingMessage("hello", "1", datetime(2026, 10, 13))
            await orchestrator.handle_message(message)

    with pytest.raises(failure, match="turn aborted"):
        asyncio.run(run_turn())

    assert idle_loop._state == "idle"
    assert idle_loop._task is not None


def test_user_active_window_still_gates_background_work_for_30_minutes(monkeypatch):
    orchestrator = object.__new__(Orchestrator)
    now = [100.0]
    monkeypatch.setattr("orchestrator.time.time", lambda: now[0])
    orchestrator._last_user_ts = now[0] - 1800
    orchestrator.idle_loop = object.__new__(IdleLoop)
    orchestrator.idle_loop._state = "idle"
    orchestrator.idle_loop._task = None

    assert not orchestrator._user_active()
    orchestrator._mark_user_active()
    assert orchestrator.idle_loop._state == "conversation"
    now[0] += 1799
    assert orchestrator._user_active()
    now[0] += 1
    assert not orchestrator._user_active()


def test_persistence_failures_are_logged_to_error_observability(monkeypatch, caplog):
    events = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(db, "execute", fail)
    monkeypatch.setattr(db, "log_event", lambda *args: events.append(args))
    handler = object.__new__(MessageHandler)
    handler.lzg = SimpleNamespace(save_kzg_turn=fail)
    db_handler = log_observability.DBLogHandler()
    logger = logging.getLogger("core.message_handler")
    logger.addHandler(db_handler)
    try:
        with caplog.at_level(logging.ERROR, logger="core.message_handler"):
            handler._persist_msg("user", "hi", channel="web")
    finally:
        logger.removeHandler(db_handler)

    assert sum(record.levelno == logging.ERROR for record in caplog.records) == 2
    assert len(events) == 2


def test_voice_reminder_does_not_mutate_nested_source_messages():
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": "Wie war mein Puls?",
            "annotations": {"source": ["chat", {"turn": 4}]},
        }],
    }]
    original = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": "Wie war mein Puls?",
            "annotations": {"source": ["chat", {"turn": 4}]},
        }],
    }]

    copied = _messages_with_voice_reminder(messages, "Wie war mein Puls?")

    assert copied[0] is not messages[0]
    assert copied[0]["content"] is not messages[0]["content"]
    assert copied[0]["content"][0] is not messages[0]["content"][0]
    assert copied[0]["content"][0]["annotations"] is not messages[0]["content"][0]["annotations"]
    assert messages == original
