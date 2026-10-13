import asyncio
from types import SimpleNamespace

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


def _async_record(target):
    async def record(text):
        target.append(text)

    return record
