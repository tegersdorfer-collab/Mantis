import asyncio
import logging
from types import SimpleNamespace

import pytest

from communication.base import IncomingMessage
from communication.discord import DiscordChannel, split_discord_message


class _Typing:
    def __init__(self, destination):
        self.destination = destination

    async def __aenter__(self):
        self.destination.typing_count += 1

    async def __aexit__(self, *_args):
        self.destination.typing_count -= 1


class _Destination:
    def __init__(self, channel_id):
        self.id = channel_id
        self.messages = []
        self.typing_count = 0

    async def send(self, text, **_kwargs):
        self.messages.append(text)

    def typing(self):
        return _Typing(self)


class _User:
    def __init__(self, user_id, *, bot=False, name="Timo"):
        self.id = user_id
        self.bot = bot
        self.name = name
        self.dm = _Destination(f"dm-{user_id}")

    async def create_dm(self):
        return self.dm

    async def send(self, text, **kwargs):
        await self.dm.send(text, **kwargs)


class _Client:
    def __init__(self, **kwargs):
        self.intents = kwargs["intents"]
        self.user = _User(999, bot=True, name="Mantis")
        self.events = {}
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.token = None
        self.reconnect = None
        self.start_error = None

    def event(self, callback):
        self.events[callback.__name__] = callback
        return callback

    async def start(self, token, *, reconnect):
        self.token = token
        self.reconnect = reconnect
        self.started.set()
        if self.start_error:
            raise self.start_error
        await self.closed.wait()

    async def close(self):
        self.closed.set()

    def get_user(self, user_id):
        if str(user_id) == "123":
            return self.allowed_user
        return None

    async def fetch_user(self, user_id):
        return self.get_user(user_id)


class _DiscordModule:
    class Intents:
        def __init__(self):
            self.guilds = False
            self.guild_messages = False
            self.dm_messages = False
            self.message_content = False

        @classmethod
        def none(cls):
            return cls()

    Client = _Client

    class LoginFailure(Exception):
        pass


class _Message:
    def __init__(self, author, channel, *, content="hi", guild=None, attachments=()):
        self.author = author
        self.channel = channel
        self.channel_id = channel.id
        self.content = content
        self.guild = guild
        self.attachments = list(attachments)


async def _start_channel(
    *, allowed_user_ids=("123",), channel_id=None, enabled=True, start_error=None
):
    def make_client(**kwargs):
        client = _Client(**kwargs)
        client.allowed_user = _User(123)
        client.start_error = start_error
        return client

    channel = DiscordChannel(
        token="  discord-secret  ",
        allowed_user_ids=set(allowed_user_ids),
        channel_id=channel_id,
        enabled=enabled,
        discord_module=_DiscordModule,
        client_factory=make_client,
    )
    await channel.start()
    if channel._task:
        await channel._client.started.wait()
    return channel, channel._client


def test_disabled_channel_does_not_import_or_start_discord(monkeypatch):
    async def run():
        def unexpected_import(_name):
            raise AssertionError("discord.py darf bei deaktiviertem Kanal nicht geladen werden")

        monkeypatch.setattr("communication.discord.importlib.import_module", unexpected_import)
        channel = DiscordChannel(enabled=False, discord_module=None)

        await channel.start()

        assert channel._client is None
        assert channel._task is None

    asyncio.run(run())


def test_missing_discord_package_keeps_process_running_and_redacts_token(monkeypatch, caplog):
    async def run():
        def missing_package(_name):
            raise ImportError("discord.py is unavailable")

        monkeypatch.setattr("communication.discord.importlib.import_module", missing_package)
        channel = DiscordChannel(token="discord-secret", enabled=True)

        with caplog.at_level(logging.ERROR, logger="communication.discord"):
            await channel.start()

        assert channel._client is None
        assert "discord.py ist nicht installiert" in caplog.text
        assert "discord-secret" not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "message_kind",
    ["foreign_user", "other_bot", "own_bot", "empty_allowlist"],
)
def test_discord_ignores_non_allowlisted_and_bot_messages(message_kind):
    async def run():
        allowed = () if message_kind == "empty_allowlist" else ("123",)
        channel, client = await _start_channel(allowed_user_ids=allowed)
        received = []
        channel.on_message(received.append)
        author = {
            "foreign_user": _User(456),
            "other_bot": _User(456, bot=True),
            "own_bot": _User(999),
            "empty_allowlist": _User(123),
        }[message_kind]

        await client.events["on_message"](_Message(author, _Destination("dm")))

        assert received == []
        await channel.stop()

    asyncio.run(run())


def test_discord_dm_delivers_incoming_message_only_from_allowed_user():
    async def run():
        channel, client = await _start_channel()
        received = []

        async def receive(message):
            received.append(message)

        channel.on_message(receive)
        raw = _Message(_User(123), _Destination("dm"), content="  hallo  ")

        await client.events["on_message"](raw)

        assert len(received) == 1
        assert isinstance(received[0], IncomingMessage)
        assert received[0].text == "  hallo  "
        assert received[0].sender_id == "123"
        assert received[0].channel == "discord"
        assert received[0].raw is raw
        await channel.stop()

    asyncio.run(run())


def test_discord_server_messages_need_the_configured_channel_id():
    async def run():
        channel, client = await _start_channel(channel_id="700")
        received = []

        async def receive(message):
            received.append(message)

        channel.on_message(receive)
        author = _User(123)

        await client.events["on_message"](
            _Message(author, _Destination("701"), guild=SimpleNamespace(id=1))
        )
        await client.events["on_message"](
            _Message(author, _Destination("700"), guild=SimpleNamespace(id=1))
        )

        assert [message.channel for message in received] == ["discord"]
        await channel.stop()

    asyncio.run(run())


def test_discord_ignores_server_messages_without_configured_channel_id():
    async def run():
        channel, client = await _start_channel()
        received = []

        async def receive(message):
            received.append(message)

        channel.on_message(receive)
        await client.events["on_message"](
            _Message(_User(123), _Destination("700"), guild=SimpleNamespace(id=1))
        )

        assert received == []
        await channel.stop()

    asyncio.run(run())


def test_discord_attachment_gets_a_friendly_text_only_reply():
    async def run():
        channel, client = await _start_channel()
        destination = _Destination("dm")
        raw = _Message(_User(123), destination, content="", attachments=[object()])

        await client.events["on_message"](raw)

        assert len(destination.messages) == 1
        assert "noch nicht" in destination.messages[0]
        await channel.stop()

    asyncio.run(run())


def test_discord_long_messages_split_at_limit_and_keep_code_fences_balanced():
    async def run():
        channel, client = await _start_channel()
        body = "Zeile\n" * 500
        text = f"```python\n{body}```"

        await channel.send(text)

        chunks = client.allowed_user.dm.messages
        assert len(chunks) > 1
        assert all(len(chunk) <= 2000 for chunk in chunks)
        assert all(chunk.count("```") == 2 for chunk in chunks)
        assert all(chunk.startswith("```python\n") and chunk.endswith("```") for chunk in chunks)
        await channel.stop()

    asyncio.run(run())


def test_discord_code_block_splitting_preserves_python_indentation():
    text = f"```python\n{'x' * 1970}\n    keep_indent()\n{'y' * 2000}```"

    chunks = split_discord_message(text)

    assert any(chunk.startswith("```python\n    keep_indent()") for chunk in chunks)
    assert all(len(chunk) <= 2000 for chunk in chunks)


def test_discord_markdown_tables_are_sent_as_readable_lists():
    async def run():
        channel, client = await _start_channel()

        await channel.send("| Name | Wert |\n|---|---|\n| Fokus | 3 |")

        assert client.allowed_user.dm.messages == ["• Name: Fokus; Wert: 3"]
        await channel.stop()

    asyncio.run(run())


def test_discord_typing_indicator_runs_until_the_reply_is_sent():
    async def run():
        channel, _client = await _start_channel()
        destination = _Destination("dm")
        route = channel.for_message(_Message(_User(123), destination))

        await route.send_typing()
        assert destination.typing_count == 1
        await route.send("fertige Antwort")

        assert destination.typing_count == 0
        assert destination.messages == ["fertige Antwort"]
        await channel.stop()

    asyncio.run(run())


def test_discord_ready_log_includes_bot_name_not_token(caplog):
    async def run():
        channel, client = await _start_channel()

        with caplog.at_level(logging.INFO, logger="communication.discord"):
            await client.events["on_ready"]()

        assert "Discord eingeloggt als Mantis" in caplog.text
        assert "discord-secret" not in caplog.text
        await channel.stop()

    asyncio.run(run())


def test_discord_uses_minimal_intents_and_reconnects():
    async def run():
        channel, client = await _start_channel(channel_id="700")

        assert client.intents.dm_messages is True
        assert client.intents.guilds is True
        assert client.intents.guild_messages is True
        assert client.intents.message_content is True
        assert client.token == "discord-secret"
        assert client.reconnect is True
        await channel.stop()

    asyncio.run(run())


def test_discord_dm_only_does_not_request_guild_or_message_content_intents():
    async def run():
        channel, client = await _start_channel()

        assert client.intents.dm_messages is True
        assert client.intents.guilds is False
        assert client.intents.guild_messages is False
        assert client.intents.message_content is False
        await channel.stop()

    asyncio.run(run())


def test_invalid_discord_token_is_redacted_and_does_not_escape(caplog):
    async def run():
        channel, client = await _start_channel(
            start_error=_DiscordModule.LoginFailure("discord-secret leaked")
        )
        with caplog.at_level(logging.ERROR, logger="communication.discord"):
            await channel._task

        assert "discord-secret" not in caplog.text
        assert "LoginFailure" in caplog.text
        await channel.stop()

    asyncio.run(run())
