"""Finale Telegram-Edits dürfen keine unvollständige Antwort stehen lassen."""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from communication.telegram import TelegramChannel
from core.message_handler import MessageHandler, PendingInboxDeletion


class _FakeBot:
    def __init__(self, edit_results):
        self.edit_results = list(edit_results)
        self.edits = []
        self.sent = []
        self.deleted = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        result = self.edit_results.pop(0)
        if isinstance(result, Exception):
            raise result

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=99)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


def _channel(bot):
    channel = TelegramChannel(token="test-token")
    channel._app = SimpleNamespace(bot=bot)
    channel._chat_id = "42"
    return channel


def test_final_markdown_edit_retries_as_plain_text():
    bot = _FakeBot([ValueError("can't parse Markdown"), None])

    asyncio.run(_channel(bot).edit_message(7, "Finale Antwort", markdown=True))

    assert [call.get("parse_mode") for call in bot.edits] == ["Markdown", None]
    assert len(bot.sent) == 0
    assert len(bot.deleted) == 0


def test_final_answer_is_sent_as_new_message_when_both_edits_fail():
    bot = _FakeBot([ValueError("bad Markdown"), RuntimeError("edit failed")])

    asyncio.run(_channel(bot).edit_message(7, "Finale Antwort", markdown=True))

    assert len(bot.edits) == 2
    assert bot.sent == [{"chat_id": "42", "text": "Finale Antwort"}]
    assert bot.deleted == [{"chat_id": "42", "message_id": 7}]


def test_stop_skips_updater_when_polling_is_not_running():
    channel = TelegramChannel(token="test-token")
    updater = SimpleNamespace(running=False, stop=AsyncMock())
    app = SimpleNamespace(updater=updater, stop=AsyncMock(), shutdown=AsyncMock())
    channel._app = app

    asyncio.run(channel.stop())

    updater.stop.assert_not_awaited()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()


def test_stop_continues_application_shutdown_when_updater_stop_fails(caplog):
    channel = TelegramChannel(token="test-token")
    updater = SimpleNamespace(running=True, stop=AsyncMock(side_effect=RuntimeError("not running")))
    app = SimpleNamespace(updater=updater, stop=AsyncMock(), shutdown=AsyncMock())
    channel._app = app

    with caplog.at_level(logging.WARNING, logger="communication.telegram"):
        asyncio.run(channel.stop())

    updater.stop.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()
    assert "Updater" in caplog.text and "RuntimeError" in caplog.text


def test_telegram_photo_reserves_user_turn_before_download(monkeypatch):
    events = []
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    received = []

    class _PendingRemote:
        def __init__(self):
            self.pending = {"123": object()}

        def begin_user_turn(self, _message):
            self.pending.clear()

    remote = _PendingRemote()
    handler = object.__new__(MessageHandler)
    handler._incoming_message_sequence = 0
    handler._pending_inbox_deletions = {
        "123": PendingInboxDeletion(
            item_id=7,
            channel_id="discord-dm-123",
            prompt_message_id="delete-prompt",
            created_at=0,
        ),
    }
    handler.agent_remote = remote

    class _PhotoFile:
        async def download_to_drive(self, path):
            events.append("download")
            download_started.set()
            await release_download.wait()
            Path(path).write_bytes(b"image")

    class _PhotoBot(_FakeBot):
        async def send_chat_action(self, **_kwargs):
            events.append("typing")

        async def get_file(self, _file_id):
            events.append("get_file")
            return _PhotoFile()

    channel = _channel(_PhotoBot([]))
    channel._allowed = {"123"}

    def reserve_turn(message):
        context = handler.begin_incoming_message(message)
        events.append(("turn-start", message.channel))
        return context

    # Der synchrone Eingangs-Hook muss vor dem ersten Await des Foto-Handlers laufen.
    channel._message_start_handler = reserve_turn

    async def receive(message):
        received.append(message)

    channel.on_message(receive)
    update = SimpleNamespace(message=SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat_id=42,
        photo=[SimpleNamespace(file_id="photo-1", file_size=12)],
        caption="Was ist darauf zu sehen?",
    ))
    monkeypatch.setattr(
        "communication.telegram._describe_image",
        AsyncMock(return_value="🖼️ BILD: Eine Tasse"),
    )

    async def run():
        task = asyncio.create_task(
            channel._handle_photo(update, SimpleNamespace(bot=channel._app.bot))
        )
        try:
            await asyncio.wait_for(download_started.wait(), timeout=1)
            assert events[:2] == [("turn-start", "telegram"), "typing"]
            assert handler._incoming_message_sequence == 1
            assert remote.pending == {}
            assert handler._pending_inbox_deletions == {}
        finally:
            release_download.set()
            await task

    asyncio.run(run())

    assert len(received) == 1
    assert received[0].ingress_context == (1, None)
