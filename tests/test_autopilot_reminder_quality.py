import asyncio
from types import SimpleNamespace

from communication.discord import DiscordChannel
from core.autopilot import Autopilot


def test_short_due_reminder_bypasses_generated_message_quality_filter(monkeypatch):
    sent = []
    delivered = []

    class Reminders:
        def get_due(self):
            delivered.append("Müll")  # get_due() markiert den Reminder als zugestellt.
            return [SimpleNamespace(text="Müll")]

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

    assert delivered == ["Müll"]
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


def _async_record(target):
    async def record(text):
        target.append(text)

    return record
