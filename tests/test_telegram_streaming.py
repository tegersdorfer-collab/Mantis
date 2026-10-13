"""Finale Telegram-Edits dürfen keine unvollständige Antwort stehen lassen."""
import asyncio
from types import SimpleNamespace

from communication.telegram import TelegramChannel


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
