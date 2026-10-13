import asyncio
from types import SimpleNamespace

import pytest

from communication.discord import DiscordChannel
from core.autopilot import Autopilot


def test_short_due_reminder_bypasses_generated_message_quality_filter(monkeypatch):
    sent = []
    delivered = []

    class Reminders:
        def get_due(self):
            return [SimpleNamespace(id=11, text="Müll")]

        def mark_sent(self, reminder_id):
            delivered.append(reminder_id)

        def mark_delivery_failed(self, _reminder_id):
            raise AssertionError("Der kurze Reminder sollte zugestellt werden")

        def release_claim(self, _reminder_id):
            pass

    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=Reminders(),
        channel=SimpleNamespace(send=_async_record(sent)),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
    monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args: 0)

    asyncio.run(autopilot.tick())

    assert delivered == [11]
    assert sent == ["⏰ Erinnerung: Müll"]


def test_failed_reminder_delivery_is_retried_on_the_next_tick(monkeypatch):
    from tools.reminders import Reminder

    reminder = Reminder(id=17, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.claimed = False
            self.failed_ids = []

        def get_due(self):
            if self.claimed:
                return []
            self.claimed = True
            return [reminder]

        def mark_delivery_failed(self, reminder_id):
            self.failed_ids.append(reminder_id)
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

    reminders = Reminders()
    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=reminders,
        channel=DiscordChannel(enabled=False, allowed_user_ids=("123",)),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
    monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args: 0)

    assert asyncio.run(autopilot.tick()) is False
    assert reminders.failed_ids == [17]
    assert asyncio.run(autopilot.tick()) is False
    assert reminders.failed_ids == [17, 17]


def test_repeated_failed_reminder_delivery_stops_after_store_limit(monkeypatch):
    from tools.reminders import Reminder

    reminder = Reminder(id=23, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.failures = 0
            self.claimed = False

        def get_due(self):
            if self.claimed or self.failures >= 3:
                return []
            self.claimed = True
            return [reminder]

        def mark_delivery_failed(self, reminder_id):
            assert reminder_id == 23
            self.failures += 1
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

    reminders = Reminders()
    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=reminders,
        channel=DiscordChannel(enabled=False, allowed_user_ids=("123",)),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
    monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args: 0)

    for _ in range(4):
        asyncio.run(autopilot.tick())

    assert reminders.failures == 3


def test_transient_transport_error_retries_reminder_and_succeeds(monkeypatch):
    from tools.reminders import Reminder

    reminder = Reminder(id=31, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.claimed = False
            self.failed_ids = []

        def get_due(self):
            if self.claimed:
                return []
            self.claimed = True
            return [reminder]

        def mark_delivery_failed(self, reminder_id):
            self.failed_ids.append(reminder_id)
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

    attempts = []

    async def send(_text):
        attempts.append("send")
        if len(attempts) == 1:
            raise OSError("transport secret must not be logged")

    reminders = Reminders()
    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=reminders,
        channel=SimpleNamespace(send=send),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
    monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args: 0)

    assert asyncio.run(autopilot.tick()) is False
    assert asyncio.run(autopilot.tick()) is True
    assert reminders.failed_ids == [31]
    assert attempts == ["send", "send"]


def test_cancelled_reminder_send_releases_claim_and_success_is_not_repeated(monkeypatch):
    from tools.reminders import Reminder

    reminder = Reminder(id=41, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.claimed = False
            self.sent = False

        def get_due(self):
            if self.claimed or self.sent:
                return []
            self.claimed = True
            return [reminder]

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False
            self.sent = True

        def mark_delivery_failed(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

    async def run():
        reminders = Reminders()
        send_started = asyncio.Event()
        attempts = []
        delivered = []

        async def send(text):
            attempts.append(text)
            if len(attempts) == 1:
                send_started.set()
                await asyncio.Future()
            delivered.append(text)

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=reminders,
            channel=SimpleNamespace(send=send),
            proactive=None,
            tracker=SimpleNamespace(record_sent=lambda: None),
            identity="",
        )
        monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
        monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
        monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
        monkeypatch.setattr("core.push.send_push", lambda *_args: 0)
        autopilot._tick_deep_execution = lambda: _async_false()

        tick = asyncio.create_task(autopilot.tick())
        await send_started.wait()
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick

        assert reminders.claimed is False
        assert await autopilot.tick() is True
        assert reminders.sent is True
        assert await autopilot.tick() is False
        assert attempts == ["⏰ Erinnerung: Termin"] * 2
        assert delivered == ["⏰ Erinnerung: Termin"]

    asyncio.run(run())


def test_cancelled_send_releases_unprocessed_reminder_claims(monkeypatch):
    from tools.reminders import Reminder

    due = [
        Reminder(id=61, text="Erster Termin", remind_at=None, sent=False),
        Reminder(id=62, text="Zweiter Termin", remind_at=None, sent=False),
    ]

    class Reminders:
        def __init__(self):
            self.claimed_ids = set()

        def get_due(self):
            self.claimed_ids = {reminder.id for reminder in due}
            return due

        def mark_sent(self, reminder_id):
            self.claimed_ids.discard(reminder_id)

        def mark_delivery_failed(self, reminder_id):
            self.claimed_ids.discard(reminder_id)

        def release_claim(self, reminder_id):
            self.claimed_ids.discard(reminder_id)

    async def run():
        reminders = Reminders()
        send_started = asyncio.Event()

        async def send(_text):
            send_started.set()
            await asyncio.Future()

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=reminders,
            channel=SimpleNamespace(send=send),
            proactive=None,
            tracker=SimpleNamespace(record_sent=lambda: None),
            identity="",
        )
        monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
        monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
        monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
        monkeypatch.setattr("core.push.send_push", lambda *_args: 0)
        autopilot._tick_deep_execution = lambda: _async_false()

        tick = asyncio.create_task(autopilot.tick())
        await send_started.wait()
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick

        assert reminders.claimed_ids == set()

    asyncio.run(run())


def test_oversized_reminder_is_truncated_before_discord_can_split_it(monkeypatch):
    from tools.reminders import Reminder

    class Destination:
        def __init__(self):
            self.messages = []

        async def send(self, text):
            self.messages.append(text)

    async def run():
        destination = Destination()
        discord = DiscordChannel(enabled=False, allowed_user_ids=("123",))
        route = discord.for_message(SimpleNamespace(channel=destination))
        reminder = Reminder(id=52, text="x" * 2500, remind_at=None, sent=False)

        class Reminders:
            def get_due(self):
                return [reminder]

            def mark_sent(self, reminder_id):
                assert reminder_id == reminder.id

            def mark_delivery_failed(self, reminder_id):
                assert reminder_id == reminder.id

            def release_claim(self, reminder_id):
                assert reminder_id == reminder.id

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=Reminders(),
            channel=route,
            reminder_channel=route,
            proactive=None,
            tracker=SimpleNamespace(record_sent=lambda: None),
            identity="",
        )
        monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
        monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
        monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
        monkeypatch.setattr("core.push.send_push", lambda *_args: 0)
        autopilot._tick_deep_execution = lambda: _async_false()

        assert await autopilot.tick() is True
        assert len(destination.messages) == 1
        assert len(destination.messages[0]) <= 2000
        assert destination.messages[0].endswith("…")

    asyncio.run(run())


async def _async_false():
    return False


def _async_record(target):
    async def record(text):
        target.append(text)

    return record
