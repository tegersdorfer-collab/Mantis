"""Discord Bot als zusätzlicher, optionaler Kommunikationskanal."""
import asyncio
import importlib
import logging
import re
import unicodedata
from datetime import datetime

from communication.base import (
    CommunicationChannel, IncomingAttachment, IncomingMessage, MessageHandler as MH,
)
import config

log = logging.getLogger(__name__)

_MESSAGE_LIMIT = 2000
_UNSUPPORTED_ATTACHMENT = (
    "Ich kann in der Merk-Inbox nur Bilder verarbeiten. Für andere Anhänge oder "
    "Sprachnachrichten schreib mir den Inhalt bitte als Text."
)
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGES = 4
_CODE_BLOCK = re.compile(r"(```[^\n]*\n.*?```)", re.DOTALL)


class _DiscordVoiceWarningFilter(logging.Filter):
    """Unterdrückt nur Hinweise zu nicht installierten Voice-Abhängigkeiten."""

    _MESSAGES = ("pynacl is not installed", "davey is not installed")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "discord.client" or record.levelno != logging.WARNING:
            return True
        message = record.getMessage().casefold()
        return not any(fragment in message for fragment in self._MESSAGES)


def _install_voice_warning_filter() -> None:
    discord_client_log = logging.getLogger("discord.client")
    if not any(isinstance(item, _DiscordVoiceWarningFilter) for item in discord_client_log.filters):
        discord_client_log.addFilter(_DiscordVoiceWarningFilter())


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(
        cell and set(cell) <= {"-", ":"} and "-" in cell
        for cell in cells
    )


def _format_markdown_tables(text: str) -> str:
    """Formatiert Tabellen außerhalb von Codeblöcken in Discord-Listen um."""
    pieces = _CODE_BLOCK.split(text)
    return "".join(
        piece if index % 2 else _format_markdown_tables_plain(piece)
        for index, piece in enumerate(pieces)
    )


def _format_markdown_tables_plain(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if (
            index + 1 < len(lines)
            and "|" in lines[index]
            and _is_table_separator(lines[index + 1])
        ):
            headers = _table_cells(lines[index])
            source_lines = [lines[index], lines[index + 1]]
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index]:
                source_lines.append(lines[index])
                cells = _table_cells(lines[index])
                fields = [
                    f"{header}: {value}"
                    for header, value in zip(headers, cells)
                    if header and value
                ]
                if fields:
                    rows.append("• " + "; ".join(fields))
                index += 1
            if any("`" in line for line in source_lines):
                output.extend(source_lines)
            else:
                output.extend(rows or ["• " + "; ".join(headers)])
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output)


def _is_grapheme_extension(char: str) -> bool:
    codepoint = ord(char)
    return (
        char == "\u200d"
        or unicodedata.category(char).startswith("M")
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _safe_split_index(text: str, index: int) -> int:
    """Zieht eine Grenze zurück, wenn sie eine Emoji-/Graphemfolge trennt."""
    while 0 < index < len(text):
        if _is_regional_indicator(text[index - 1]) and _is_regional_indicator(text[index]):
            run_start = index - 1
            while run_start > 0 and _is_regional_indicator(text[run_start - 1]):
                run_start -= 1
            if (index - run_start) % 2:
                index -= 1
                continue
        if _is_grapheme_extension(text[index]) or text[index - 1] == "\u200d":
            index -= 1
            continue
        break
    return index


def _split_plain(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = max(
            remaining.rfind("\n\n", 0, limit + 1),
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if boundary <= 0:
            boundary = limit
        boundary = _safe_split_index(remaining, boundary)
        if boundary <= 0:
            boundary = limit
        chunk = remaining[:boundary].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            boundary = limit
        chunks.append(chunk)
        remaining = remaining[boundary:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_code_body(body: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remaining = body
    while len(remaining) > limit:
        newline = remaining.rfind("\n", 0, limit + 1)
        end = newline + 1 if newline >= 0 else limit
        end = _safe_split_index(remaining, end)
        if end <= 0:
            end = limit
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_code_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    newline = block.find("\n")
    opening = block[:newline + 1]
    body = block[newline + 1:-3]
    closing = "```"
    body_limit = limit - len(opening) - len(closing) - 1
    if body_limit <= 0:
        return _split_plain(block, limit)
    chunks = []
    for part in _split_code_body(body, body_limit):
        separator = "" if part.endswith("\n") else "\n"
        chunks.append(f"{opening}{part}{separator}{closing}")
    return chunks


def split_discord_message(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Teilt Antworten an Absätzen und Codeblöcken entlang in Discord-Nachrichten."""
    prepared = _format_markdown_tables(text)
    pieces = _CODE_BLOCK.split(prepared)
    chunks: list[str] = []
    for index, piece in enumerate(pieces):
        if not piece:
            continue
        part_chunks = (
            _split_code_block(piece, limit)
            if index % 2
            else _split_plain(piece, limit)
        )
        chunks.extend(part_chunks)
    return chunks or [""]


class DiscordChannel(CommunicationChannel):
    """Discord-Gateway-Kanal; `discord.py` wird nur bei aktivem Kanal geladen."""

    supports_streaming = False

    def __init__(
        self,
        token: str | None = None,
        allowed_user_ids=None,
        channel_id: str | None = None,
        enabled: bool | None = None,
        *,
        discord_module=None,
        client_factory=None,
    ):
        self._token = (config.DISCORD_BOT_TOKEN if token is None else token).strip()
        configured_ids = config.DISCORD_ALLOWED_USER_IDS if allowed_user_ids is None else allowed_user_ids
        self._allowed_user_ids = tuple(dict.fromkeys(
            str(user_id).strip() for user_id in configured_ids if str(user_id).strip()
        ))
        configured_channel = config.DISCORD_CHANNEL_ID if channel_id is None else channel_id
        self._channel_id = str(configured_channel).strip() or None
        self._enabled = config.DISCORD_ENABLED if enabled is None else enabled
        self._discord_module = discord_module
        self._client_factory = client_factory
        self._client = None
        self._task: asyncio.Task | None = None
        self._handler: MH | None = None

    def on_message(self, handler: MH) -> None:
        self._handler = handler

    def for_message(self, message):
        return _DiscordRoute(self, destination=message.channel)

    def for_first_allowed_user(self):
        user_id = self._allowed_user_ids[0] if self._allowed_user_ids else None
        return _DiscordRoute(self, user_id=user_id)

    async def _default_route(self):
        return self.for_first_allowed_user()

    async def send(self, text: str) -> str | None:
        return await (await self._default_route()).send(text)

    async def send_plain(self, text: str) -> None:
        """Sendet einen unformatierten Text ohne Umwandlung oder Aufteilung."""
        await (await self._default_route()).send_plain(text)

    async def send_typing(self) -> None:
        await (await self._default_route()).send_typing()

    async def start_message(self, text: str = "…") -> int | None:
        return None

    async def edit_message(self, message_id: int, text: str, markdown: bool = False) -> None:
        return None

    def _load_discord(self):
        return self._discord_module or importlib.import_module("discord")

    async def start(self) -> None:
        if not self._enabled:
            log.info("Discord-Kanal inaktiv (DISCORD_ENABLED=False)")
            return
        if not self._token:
            log.error("Discord-Kanal inaktiv: DISCORD_BOT_TOKEN fehlt")
            return
        _install_voice_warning_filter()
        try:
            self._discord_module = self._load_discord()
        except ImportError:
            log.error("Discord-Kanal inaktiv: discord.py ist nicht installiert")
            return

        intents = self._discord_module.Intents.none()
        intents.dm_messages = True
        intents.guilds = True
        if self._channel_id:
            intents.guild_messages = True
            intents.message_content = True
        factory = self._client_factory or self._discord_module.Client
        self._client = factory(intents=intents)
        self._register_events()
        self._task = asyncio.create_task(self._run())
        log.info("Discord-Kanal aktiv")

    def _register_events(self) -> None:
        @self._client.event
        async def on_ready():
            user = self._client.user
            if user:
                log.info("Discord eingeloggt als %s", user.name)

        @self._client.event
        async def on_message(message):
            await self._handle_message(message)

    async def _run(self) -> None:
        try:
            await self._client.start(self._token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Discord-Anmeldung oder Verbindung fehlgeschlagen (%s); Token redigiert",
                type(exc).__name__,
            )

    def _authorized(self, message) -> bool:
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return False
        own_user = getattr(self._client, "user", None)
        if own_user is not None and str(author.id) == str(own_user.id):
            return False
        if str(author.id) not in self._allowed_user_ids:
            return False
        if getattr(message, "guild", None) is None:
            return True
        return self._channel_id is not None and str(message.channel.id) == self._channel_id

    async def _handle_message(self, message) -> None:
        if not self._authorized(message):
            return
        route = self.for_message(message)
        raw_attachments = tuple(getattr(message, "attachments", ()) or ())
        incoming_attachments = []
        rejected = 0
        oversized = False
        for attachment in raw_attachments:
            content_type = str(getattr(attachment, "content_type", "") or "").split(";", 1)[0].casefold()
            if content_type not in _SUPPORTED_IMAGE_TYPES:
                rejected += 1
                continue
            if len(incoming_attachments) >= _MAX_IMAGES:
                rejected += 1
                continue
            try:
                size = int(getattr(attachment, "size", -1))
            except (TypeError, ValueError):
                size = -1
            if size < 0 or size > _MAX_IMAGE_BYTES:
                oversized = oversized or size > _MAX_IMAGE_BYTES
                rejected += 1
                continue
            reader = getattr(attachment, "read", None)
            if not callable(reader):
                rejected += 1
                continue
            try:
                data = await reader()
            except Exception:
                rejected += 1
                continue
            if not isinstance(data, (bytes, bytearray)) or not data or len(data) > _MAX_IMAGE_BYTES:
                oversized = oversized or isinstance(data, (bytes, bytearray)) and len(data) > _MAX_IMAGE_BYTES
                rejected += 1
                continue
            incoming_attachments.append(IncomingAttachment(
                filename=str(getattr(attachment, "filename", "Bild"))[:255],
                content_type=content_type,
                size=len(data),
                data=bytes(data),
            ))

        if raw_attachments and not incoming_attachments:
            notice = (
                "Bilder dürfen höchstens 10 MiB groß sein."
                if oversized else _UNSUPPORTED_ATTACHMENT
            )
            await route.send(notice)
            return

        text = str(getattr(message, "content", "") or "")
        if not text and not incoming_attachments:
            return
        reference = getattr(message, "reference", None)
        reference_id = getattr(reference, "message_id", None)
        notice = ""
        if rejected:
            parts = []
            if oversized:
                parts.append("Bilder über 10 MiB wurden ignoriert")
            if rejected and not oversized:
                parts.append("Nicht-Bild-Anhänge oder zusätzliche Bilder wurden ignoriert")
            notice = "; ".join(parts) + "."
        incoming = IncomingMessage(
            text=text,
            sender_id=str(message.author.id),
            timestamp=datetime.now(),
            raw=message,
            channel="discord",
            source_message_id=str(message.id) if getattr(message, "id", None) is not None else None,
            reply_to_message_id=str(reference_id) if reference_id is not None else None,
            attachments=tuple(incoming_attachments),
            attachment_notice=notice,
        )
        if self._handler:
            await self._handler(incoming)

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
        if self._task:
            if not self._task.done():
                self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class _DiscordRoute:
    """Antwortziel für eine konkrete Nachricht oder die Benachrichtigungs-DM."""

    def __init__(self, channel: DiscordChannel, destination=None, user_id=None):
        self._channel = channel
        self._destination = destination
        self._user_id = user_id
        self._typing_task: asyncio.Task | None = None
        self._typing_done: asyncio.Event | None = None

    async def _resolve_destination(self):
        if self._destination is not None:
            return self._destination
        if self._user_id is None or self._channel._client is None:
            return None
        user = self._channel._client.get_user(int(self._user_id))
        if user is None:
            user = await self._channel._client.fetch_user(int(self._user_id))
        if hasattr(user, "create_dm"):
            return await user.create_dm()
        return user

    async def send(self, text: str) -> str | None:
        await self._stop_typing()
        destination = await self._resolve_destination()
        if destination is None:
            log.warning("Discord-Nachricht nicht gesendet: kein erlaubter Empfänger oder keine Verbindung")
            raise ConnectionError("Discord nicht verbunden oder kein erlaubter Empfänger")
        sent_message = None
        for chunk in split_discord_message(text):
            sent_message = await destination.send(chunk)
        if isinstance(sent_message, (str, int)):
            return str(sent_message)
        message_id = getattr(sent_message, "id", None)
        return str(message_id) if message_id is not None else None

    async def send_plain(self, text: str) -> None:
        """Sendet einen einzelnen Text unverändert, ohne Markdown-Verarbeitung."""
        await self._stop_typing()
        destination = await self._resolve_destination()
        if destination is None:
            log.warning("Discord-Nachricht nicht gesendet: kein erlaubter Empfänger oder keine Verbindung")
            raise ConnectionError("Discord nicht verbunden oder kein erlaubter Empfänger")
        await destination.send(text)

    async def send_typing(self) -> None:
        destination = await self._resolve_destination()
        if destination is None:
            return
        typing = getattr(destination, "typing", None)
        if typing is None:
            return
        if self._typing_task and not self._typing_task.done():
            return
        self._typing_done = asyncio.Event()
        self._typing_task = asyncio.create_task(self._keep_typing(typing()))
        await asyncio.sleep(0)

    async def _keep_typing(self, typing_context) -> None:
        try:
            async with typing_context:
                await self._typing_done.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Discord-Tippanzeige fehlgeschlagen (%s)", type(exc).__name__)

    async def _stop_typing(self) -> None:
        if not self._typing_task:
            return
        if self._typing_done:
            self._typing_done.set()
        try:
            await self._typing_task
        except asyncio.CancelledError:
            pass
        self._typing_task = None
        self._typing_done = None

    async def stop_typing(self) -> None:
        await self._stop_typing()
