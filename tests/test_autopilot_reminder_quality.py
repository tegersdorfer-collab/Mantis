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

        def mark_delivery_failed(self, _reminder_id, _claimed_at=None):
            raise AssertionError("Der kurze Reminder sollte zugestellt werden")

        def release_claim(self, _reminder_id, _claimed_at=None):
            pass

    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=Reminders(),
        channel=SimpleNamespace(
            send=_async_record(sent),
            send_plain=_async_record(sent),
        ),
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

        def mark_delivery_failed(self, reminder_id, _claimed_at=None):
            self.failed_ids.append(reminder_id)
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id, _claimed_at=None):
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

        def mark_delivery_failed(self, reminder_id, _claimed_at=None):
            assert reminder_id == 23
            self.failures += 1
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id, _claimed_at=None):
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

        def mark_delivery_failed(self, reminder_id, _claimed_at=None):
            self.failed_ids.append(reminder_id)
            self.claimed = False

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id, _claimed_at=None):
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
        channel=SimpleNamespace(send=send, send_plain=send),
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

        def mark_delivery_failed(self, reminder_id, _claimed_at=None):
            assert reminder_id == reminder.id
            self.claimed = False

        def release_claim(self, reminder_id, _claimed_at=None):
            assert reminder_id == reminder.id
            self.claimed = False

    async def run():
        reminders = Reminders()
        send_started = asyncio.Event()
        attempts = []
        delivered = []

        async def send_plain(text):
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
            channel=SimpleNamespace(send=send_plain, send_plain=send_plain),
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

        def mark_delivery_failed(self, reminder_id, _claimed_at=None):
            self.claimed_ids.discard(reminder_id)

        def release_claim(self, reminder_id, _claimed_at=None):
            self.claimed_ids.discard(reminder_id)

    async def run():
        reminders = Reminders()
        send_started = asyncio.Event()

        async def send_plain(_text):
            send_started.set()
            await asyncio.Future()

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=reminders,
            channel=SimpleNamespace(send=send_plain, send_plain=send_plain),
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

            def mark_delivery_failed(self, reminder_id, _claimed_at=None):
                assert reminder_id == reminder.id

            def release_claim(self, reminder_id, _claimed_at=None):
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


@pytest.mark.parametrize("channel_kind", ["discord", "telegram"])
def test_markdown_reminder_is_sent_as_one_plain_message(channel_kind, monkeypatch):
    from communication.telegram import TelegramChannel
    from tools.reminders import Reminder

    body = "| Name | Wert |\n|---|---|\n| Fokus | 3 |\n```python\nprint('ok')\n```"
    reminder = Reminder(id=53, text=body, remind_at=None, sent=False)

    class Reminders:
        def get_due(self):
            return [reminder]

        def mark_sent(self, reminder_id):
            assert reminder_id == reminder.id

        def mark_delivery_failed(self, *_args):
            raise AssertionError("Der Klartextversand ist erfolgreich")

        def release_claim(self, *_args):
            raise AssertionError("Ein bestätigter Reminder darf nicht freigegeben werden")

    async def run():
        if channel_kind == "discord":
            destination = SimpleNamespace(messages=[])

            async def send(text):
                destination.messages.append(text)

            destination.send = send
            discord = DiscordChannel(enabled=False, allowed_user_ids=("123",))
            channel = discord.for_message(SimpleNamespace(channel=destination))
            calls = destination.messages
        else:
            calls = []

            class Bot:
                async def send_message(self, **kwargs):
                    calls.append(kwargs)

            channel = TelegramChannel()
            channel._app = SimpleNamespace(bot=Bot())
            channel._chat_id = "123"

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=Reminders(),
            channel=channel,
            reminder_channel=channel,
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
        assert len(calls) == 1
        expected = f"⏰ Erinnerung: {body}"
        if channel_kind == "discord":
            assert calls == [expected]
        else:
            assert calls[0]["text"] == expected
            assert calls[0].get("parse_mode") is None

    asyncio.run(run())


def test_successful_send_is_not_retried_or_released_when_mark_sent_raises(
    monkeypatch, caplog
):
    from tools.reminders import Reminder

    reminder = Reminder(id=54, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.claimed = False
            self.failed = []
            self.released = []

        def get_due(self):
            if self.claimed:
                return []
            self.claimed = True
            return [reminder]

        def mark_sent(self, _reminder_id):
            raise OSError("private transport detail")

        def mark_delivery_failed(self, reminder_id, _claimed_at):
            self.failed.append(reminder_id)

        def release_claim(self, reminder_id, _claimed_at):
            self.released.append(reminder_id)
            self.claimed = False

    reminders = Reminders()
    calls = []

    async def send_plain(text):
        calls.append(text)

    autopilot = Autopilot(
        llm=None,
        lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
        dashboard=None,
        reminders=reminders,
        channel=SimpleNamespace(send_plain=send_plain),
        proactive=None,
        tracker=SimpleNamespace(record_sent=lambda: None),
        identity="",
    )
    monkeypatch.setattr("config.AUTONOMOUS_MESSAGES_ENABLED", False)
    monkeypatch.setattr("core.autopilot.db.execute", lambda *_args: None)
    monkeypatch.setattr("core.autopilot.log_event", lambda *_args: None)
    monkeypatch.setattr("core.push.send_push", lambda *_args: 0)
    autopilot._tick_deep_execution = lambda: _async_false()

    asyncio.run(autopilot.tick())
    asyncio.run(autopilot.tick())

    assert calls == ["⏰ Erinnerung: Termin"]
    assert reminders.failed == []
    assert reminders.released == []
    assert "private transport detail" not in caplog.text


def test_cancel_after_successful_send_keeps_claim_and_does_not_retry(monkeypatch):
    from tools.reminders import Reminder

    reminder = Reminder(id=55, text="Termin", remind_at=None, sent=False)

    class Reminders:
        def __init__(self):
            self.claimed = False
            self.sent = False
            self.failures = []
            self.releases = []

        def get_due(self):
            if self.claimed or self.sent:
                return []
            self.claimed = True
            return [reminder]

        def mark_sent(self, _reminder_id):
            self.sent = True
            self.claimed = False

        def mark_delivery_failed(self, reminder_id, _claimed_at):
            self.failures.append(reminder_id)

        def release_claim(self, reminder_id, _claimed_at):
            self.releases.append(reminder_id)
            self.claimed = False

    async def run():
        reminders = Reminders()
        calls = []

        async def send_plain(text):
            calls.append(text)
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)

        autopilot = Autopilot(
            llm=None,
            lzg=SimpleNamespace(save_kzg_turn=lambda *_args: None),
            dashboard=None,
            reminders=reminders,
            channel=SimpleNamespace(send_plain=send_plain),
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
        with pytest.raises(asyncio.CancelledError):
            await tick
        await asyncio.sleep(0)

        assert reminders.sent is True
        assert reminders.failures == []
        assert reminders.releases == []
        assert calls == ["⏰ Erinnerung: Termin"]

    asyncio.run(run())


async def _async_false():
    return False


def _async_record(target):
    async def record(text):
        target.append(text)

    return record
