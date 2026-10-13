"""
Abstrakte Basisklasse für Kommunikationskanäle.
Implementiere für: Telegram, WhatsApp, Dashboard-WebSocket, CLI, etc.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Awaitable


@dataclass
class IncomingAttachment:
    filename: str
    content_type: str
    size: int
    data: bytes


@dataclass
class IncomingMessage:
    text: str
    sender_id: str
    timestamp: datetime
    raw: object | None = None   # Original Message-Objekt falls gebraucht
    channel: str = "telegram"
    source_message_id: str | None = None
    reply_to_message_id: str | None = None
    attachments: tuple[IncomingAttachment, ...] = ()
    attachment_notice: str = ""


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class CommunicationChannel(ABC):

    @abstractmethod
    async def send(self, text: str) -> None:
        """Nachricht an Timo senden."""
        ...

    @abstractmethod
    async def send_typing(self) -> None:
        """'Tipp...' Indikator anzeigen."""
        ...

    @abstractmethod
    def on_message(self, handler: MessageHandler) -> None:
        """Callback registrieren der bei eingehender Nachricht aufgerufen wird."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Kanal starten (polling / webhook / etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Kanal sauber beenden."""
        ...

    # ── Optionales Streaming (Default: nicht unterstützt) ──────────────────────

    supports_streaming: bool = False

    async def start_message(self, text: str = "…") -> int | None:
        return None

    async def edit_message(self, message_id: int, text: str, markdown: bool = False) -> None:
        return None
