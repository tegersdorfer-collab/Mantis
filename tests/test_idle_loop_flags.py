import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from core.idle_loop import IdleLoop, log_background_job_status


def _loop(chat_task_llm=None, bg_llm=None):
    async def noop(*args, **kwargs):
        return None

    async def no_memories(*args, **kwargs):
        return 0

    return IdleLoop(
        autopilot=SimpleNamespace(tick=noop, _send=noop),
        reflection=SimpleNamespace(daily_reflection=noop),
        consolidator=SimpleNamespace(consolidate_silent=noop),
        forgetting=SimpleNamespace(run=noop),
        kzg=SimpleNamespace(should_checkpoint=lambda: False),
        lzg=SimpleNamespace(),
        dashboard=SimpleNamespace(),
        thermal=SimpleNamespace(),
        reminders=SimpleNamespace(),
        proactive_engine=SimpleNamespace(),
        proactive_tracker=SimpleNamespace(),
        bg_llm=bg_llm,
        chat_llm=None,
        suggest_one=noop,
        is_user_active=lambda: False,
        pattern_detector_mod=SimpleNamespace(update_memories=no_memories),
        generate_insight_task=noop,
        chat_task_llm=chat_task_llm,
    )


def test_background_job_status_is_one_info_line_with_active_and_inactive_jobs(
    monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(config, "INSIGHTS_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_REFRESH_ENABLED", False)

    with caplog.at_level(logging.INFO, logger="core.idle_loop"):
        log_background_job_status()

    records = [record for record in caplog.records if record.name == "core.idle_loop"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "Insight Engine" in records[0].getMessage()
    assert "News-Globus-Refresh" in records[0].getMessage()
    assert "Forgetting und Chat-Pruning" in records[0].getMessage()


def test_idle_loop_calls_autopilot_tick_even_when_autonomous_messages_are_off(monkeypatch):
    calls = []

    async def tick():
        calls.append(True)

    loop = _loop()
    loop.autopilot.tick = tick
    monkeypatch.setattr(config, "AUTONOMOUS_MESSAGES_ENABLED", False)

    asyncio.run(loop._tick_autopilot())

    assert calls == [True]


@pytest.mark.parametrize(
    ("flag", "tick_name"),
    [
        ("DAILY_REFLECTION_ENABLED", "_tick_reflection"),
        ("NEWS_REFRESH_ENABLED", "_tick_news"),
        ("TRAINING_PLAN_AUTOGEN_ENABLED", "_tick_plan"),
        ("PATTERN_DETECTION_ENABLED", "_tick_patterns"),
        ("INSIGHTS_ENABLED", "_tick_insights"),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_idle_job_flag_controls_its_tick(monkeypatch, flag, tick_name, enabled):
    from core import news_globe
    from domains import fitness, plan_generator
    import core.idle_loop as idle_loop_module

    calls = []

    async def record(*args, **kwargs):
        calls.append(True)
        return 0

    loop = _loop()
    monkeypatch.setattr(config, flag, enabled, raising=False)
    loop.reflection.daily_reflection = record
    loop.pattern_detector.update_memories = record
    loop.generate_insight = record
    monkeypatch.setattr(news_globe, "refresh", record)
    monkeypatch.setattr(fitness, "active_plan", lambda: None)
    monkeypatch.setattr(plan_generator, "needs_regen", lambda *_: True)
    monkeypatch.setattr(plan_generator, "generate_and_save", record)
    monkeypatch.setattr(idle_loop_module.db, "query", lambda *args, **kwargs: [])

    asyncio.run(getattr(loop, tick_name)())

    assert len(calls) == int(enabled)


@pytest.mark.parametrize("enabled", [False, True])
def test_memory_consolidation_flag_keeps_forgetting_and_backups_running(monkeypatch, enabled):
    import core.idle_loop as idle_loop_module

    calls = []

    async def record_async(*args, **kwargs):
        calls.append("consolidation")

    async def forgetting_run():
        calls.append("forgetting")
        return {"forgotten": 0, "chat_pruned": 0}

    async def run_in_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    loop = _loop()
    loop.consolidator.consolidate_silent = record_async
    loop.forgetting.run = forgetting_run
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_ENABLED", enabled, raising=False)
    monkeypatch.setattr(idle_loop_module.asyncio, "to_thread", run_in_thread)
    monkeypatch.setattr(idle_loop_module.backup, "maybe_run_daily", lambda: calls.append("backup"))
    monkeypatch.setattr(idle_loop_module.backup, "maybe_verify_weekly", lambda: calls.append("verify"))

    asyncio.run(loop._tick_maintenance())

    assert ("consolidation" in calls) is enabled
    assert {"forgetting", "backup", "verify"} <= set(calls)


@pytest.mark.parametrize("enabled", [False, True])
def test_task_deep_execution_has_its_own_flag_while_autonomous_messages_are_off(
    monkeypatch, enabled
):
    import core.autopilot as autopilot_module
    from core.autopilot import Autopilot

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 2, 0)

    monkeypatch.setattr(autopilot_module, "datetime", FixedDateTime)
    monkeypatch.setattr(autopilot_module.db, "query", lambda *_: [{"id": 7, "title": "Test"}])
    monkeypatch.setattr(config, "AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr(config, "TASK_DEEP_EXECUTION_ENABLED", enabled, raising=False)
    autopilot = Autopilot(
        llm=None,
        lzg=None,
        dashboard=None,
        reminders=SimpleNamespace(get_due=lambda: []),
        channel=None,
        proactive=None,
        tracker=SimpleNamespace(can_send=lambda: False),
        identity="",
        is_user_active=lambda: False,
    )
    autopilot._needs = lambda _key: False
    executed = []

    async def execute(_task):
        executed.append(True)

    autopilot._deep_execute_tick = execute

    result = asyncio.run(autopilot.tick())

    assert result is enabled
    assert executed == ([True] if enabled else [])


@pytest.mark.parametrize("use_cloud", [False, True])
def test_kzg_checkpoint_uses_chat_task_provider_when_configured(use_cloud):
    calls = []

    class FakeLLM:
        async def complete(self, prompt, max_tokens):
            calls.append((self, prompt, max_tokens))
            return "Zusammenfassung"

    local_llm = FakeLLM()
    cloud_llm = FakeLLM()
    kzg = SimpleNamespace(
        should_checkpoint=lambda: True,
        get_turns_for_summary=lambda: [SimpleNamespace(role="user", content="hallo")],
        apply_checkpoint=lambda summary, count: calls.append(("checkpoint", summary, count)),
    )
    loop = _loop(
        chat_task_llm=cloud_llm if use_cloud else None,
        bg_llm=local_llm,
    )
    loop.kzg = kzg

    asyncio.run(loop._maybe_kzg_checkpoint())

    selected = cloud_llm if use_cloud else local_llm
    assert calls[0][0] is selected
    assert calls[0][2] == 300
    assert calls[1] == ("checkpoint", "Zusammenfassung", 1)


def test_due_reminders_still_send_when_autonomous_messages_are_off(monkeypatch):
    import core.autopilot as autopilot_module
    from core.autopilot import Autopilot

    sent = []

    async def send(text):
        sent.append(text)

    class Tracker:
        def can_send(self):
            return False

        def record_sent(self):
            pass

    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_: None),
        dashboard=None,
        reminders=SimpleNamespace(
            get_due=lambda: [SimpleNamespace(id=1, text="Lang genug für den Test")],
            mark_sent=lambda _reminder_id: None,
            mark_delivery_failed=lambda _reminder_id: None,
            release_claim=lambda _reminder_id: None,
        ),
        channel=SimpleNamespace(send=send),
        proactive=None,
        tracker=Tracker(),
        identity="",
    )
    monkeypatch.setattr(config, "AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr(autopilot_module.db, "execute", lambda *args, **kwargs: None)
    monkeypatch.setattr(autopilot_module.db, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(autopilot_module.BUS, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.push.send_push", lambda *args, **kwargs: 0)

    assert asyncio.run(autopilot.tick()) is True
    assert sent == ["⏰ Erinnerung: Lang genug für den Test"]


def test_only_due_reminders_use_configured_notification_channel(monkeypatch):
    from core.autopilot import Autopilot

    sent = {"telegram": [], "discord": []}

    async def send_telegram(text):
        sent["telegram"].append(text)

    async def send_discord(text):
        sent["discord"].append(text)

    class Tracker:
        def record_sent(self):
            pass

    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_: None),
        dashboard=None,
        reminders=None,
        channel=SimpleNamespace(send=send_telegram),
        reminder_channel=SimpleNamespace(send=send_discord),
        proactive=None,
        tracker=Tracker(),
        identity="",
    )
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.autopilot.db.log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.autopilot.BUS.emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(config, "AUTONOMOUS_MESSAGES_ENABLED", True)

    async def send_messages():
        await autopilot._send("⏰ Erinnerung: Termin", kind="reminder")
        await autopilot._send("Das ist eine ausreichend lange proaktive Nachricht.")

    asyncio.run(send_messages())

    assert sent == {
        "telegram": ["Das ist eine ausreichend lange proaktive Nachricht."],
        "discord": ["⏰ Erinnerung: Termin"],
    }
