"""
Telegram Bot – primärer Kommunikationskanal.
Tauschbar gegen andere Kanäle via CommunicationChannel Interface.
Unterstützt: Text, Sprachnachrichten (→ Transkription via Whisper), Fotos (→ Claude Haiku Vision).
"""
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.constants import ChatAction

from communication.base import CommunicationChannel, IncomingMessage, MessageHandler as MH
from core.voice import transcribe_audio as _transcribe
from core.vision import describe_image as _describe_image_raw
import config

log = logging.getLogger(__name__)
_MESSAGE_LIMIT = 4096


# ── Ollama-Vision (für Fotos) ─────────────────────────────────────────────────

_VISION_PROMPT = (
    "Beschreibe dieses Bild kurz auf Deutsch. "
    "Falls es sich um Essen oder Getränke handelt, schätze die Kalorien "
    "und Makronährstoffe (Protein, Kohlenhydrate, Fett) so genau wie möglich "
    "und beginne deine Antwort mit '🍽️ MAHLZEIT:'. "
    "Sonst beginne mit '🖼️ BILD:'."
)


async def _describe_image(image_bytes: bytes) -> str:
    """Beschreibt ein Bild lokal via Ollama-Vision. Erkennt Mahlzeiten für Tracking."""
    return await _describe_image_raw(image_bytes, _VISION_PROMPT)


class TelegramChannel(CommunicationChannel):
    supports_streaming = True

    def __init__(self, token: str | None = None):
        self._token = token or config.TELEGRAM_BOT_TOKEN
        self._app: Application | None = None
        self._chat_id: str | None = config.TELEGRAM_CHAT_ID or None
        self._allowed: set[str] = set(config.TELEGRAM_ALLOWED_IDS)
        self._handler: MH | None = None

    def on_message(self, handler: MH) -> None:
        self._handler = handler

    async def send(self, text: str) -> None:
        if not self._app or not self._chat_id:
            log.warning("Telegram nicht verbunden — Nachricht nicht gesendet")
            raise ConnectionError("Telegram nicht verbunden")

        # Lange Nachrichten aufteilen (Telegram-Limit: 4096 Zeichen)
        chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            try:
                await self._app.bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                )
            except Exception:
                # Fallback: plain text wenn Markdown Fehler wirft
                await self._app.bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                )

    async def send_plain(self, text: str) -> None:
        """Sendet einen einzelnen Klartext-Text ohne Markdown und ohne Aufteilung."""
        if not self._app or not self._chat_id:
            log.warning("Telegram nicht verbunden — Nachricht nicht gesendet")
            raise ConnectionError("Telegram nicht verbunden")
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text[:_MESSAGE_LIMIT],
            parse_mode=None,
        )

    async def send_with_buttons(
        self,
        text: str,
        buttons: list[list[tuple[str, str]]],
    ) -> int | None:
        """Sendet eine Nachricht mit Inline-Keyboard.

        buttons: Liste von Zeilen, jede Zeile ist eine Liste aus (label, callback_data)-Tuples.
        Gibt message_id zurück für spätere Edits.
        """
        if not (self._app and self._chat_id):
            return None
        keyboard = [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in buttons
        ]
        try:
            msg = await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return msg.message_id
        except Exception as e:
            log.warning(f"send_with_buttons fehlgeschlagen: {e}")
            await self.send(text)
            return None

    async def send_voice(self, text: str, voice: str | None = None) -> bool:
        """Text → TTS → Telegram Sprachnachricht. Gibt True zurück bei Erfolg."""
        if not (self._app and self._chat_id):
            return False
        try:
            from core.tts import synthesize, is_available
            if not is_available():
                log.warning("TTS nicht verfügbar — sende als Text")
                await self.send(text)
                return False
            ogg = await synthesize(text, voice=voice or "de_DE-thorsten-high")
            if not ogg:
                await self.send(text)
                return False
            from io import BytesIO
            await self._app.bot.send_voice(
                chat_id=self._chat_id,
                voice=BytesIO(ogg),
                caption=None,
            )
            return True
        except Exception as e:
            log.error(f"send_voice fehlgeschlagen: {e}")
            await self.send(text)
            return False

    async def remove_buttons(self, message_id: int) -> None:
        """Entfernt Inline-Keyboard von einer Nachricht (nach Aktion)."""
        if not (self._app and self._chat_id and message_id):
            return
        try:
            await self._app.bot.edit_message_reply_markup(
                chat_id=self._chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception:
            pass

    async def send_typing(self) -> None:
        if self._app and self._chat_id:
            await self._app.bot.send_chat_action(
                chat_id=self._chat_id,
                action=ChatAction.TYPING,
            )

    # ── Streaming (Live-Edit einer Nachricht) ─────────────────────────────────

    async def start_message(self, text: str = "…") -> int | None:
        if not (self._app and self._chat_id):
            return None
        try:
            msg = await self._app.bot.send_message(chat_id=self._chat_id, text=text)
            return msg.message_id
        except Exception:
            return None

    async def edit_message(self, message_id: int, text: str, markdown: bool = False) -> None:
        if not (self._app and self._chat_id and message_id):
            return
        bot = self._app.bot
        content = text[:4096]
        try:
            await bot.edit_message_text(
                chat_id=self._chat_id, message_id=message_id, text=content,
                parse_mode="Markdown" if markdown else None,
            )
        except Exception:
            if not markdown:
                return  # Bei Zwischenständen ist ein fehlgeschlagener Edit nicht final.
            try:
                await bot.edit_message_text(
                    chat_id=self._chat_id, message_id=message_id, text=content,
                    parse_mode=None,
                )
                return
            except Exception:
                pass

            try:
                await bot.send_message(chat_id=self._chat_id, text=content)
            except Exception:
                log.error("Telegram-Finalantwort konnte nicht editiert oder neu gesendet werden")
                return

            try:
                await bot.delete_message(chat_id=self._chat_id, message_id=message_id)
            except Exception:
                log.debug("Telegram-Teilantwort konnte nach Ersatz nicht gelöscht werden")

    def _authorized(self, update: Update) -> bool:
        """Darf dieser Absender mit Mantis reden?
        Mit konfigurierter Allowlist: strikt (User- oder Chat-ID muss passen).
        Ohne Allowlist: Trust-on-first-use – der erste Absender wird für die
        Laufzeit gesperrt, alle anderen abgewiesen (statt offen für jeden)."""
        if not update.message or not update.message.from_user:
            return False
        uid = str(update.message.from_user.id)
        cid = str(update.message.chat_id)
        if self._allowed:
            return uid in self._allowed or cid in self._allowed
        # Nichts konfiguriert → auf ersten Absender sperren und laut warnen
        log.warning(
            "⚠️  Keine TELEGRAM_ALLOWED_IDS/TELEGRAM_CHAT_ID gesetzt – sperre auf "
            "ersten Absender %s. Trag die ID in .env ein!", uid,
        )
        self._allowed = {uid}
        self._chat_id = cid
        return True

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.text:
            return
        if not self._authorized(update):
            who = update.message.from_user.id if update.message.from_user else "?"
            log.warning("Nachricht von nicht autorisierter ID %s verworfen", who)
            return

        # Chat-ID des autorisierten Absenders merken (für Antworten/Proaktives)
        self._chat_id = str(update.message.chat_id)

        text = update.message.text or ""

        # URLs aus Telegram-Entities extrahieren (präziser als Regex auf Plaintext)
        urls = []
        for entity in (update.message.entities or []):
            if entity.type in ("url", "text_link"):
                if entity.type == "text_link":
                    urls.append(entity.url)
                else:
                    urls.append(text[entity.offset: entity.offset + entity.length])

        # URL-Inhalt vorab laden und als Kontext anhängen
        if urls:
            await self._app.bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)
            from tools.url_handler import fetch, format_for_llm
            url_contexts = []
            for url in urls[:2]:  # max 2 URLs pro Nachricht
                try:
                    data = await fetch(url)
                    url_contexts.append(format_for_llm(data, max_text=3000))
                    log.info(f"🔗 URL geladen: {url} ({data['type']}, {data['platform']})")
                except Exception as e:
                    log.warning(f"URL-Fetch fehlgeschlagen ({url}): {e}")
            if url_contexts:
                text = text + "\n\n[URL-Inhalt automatisch geladen]\n" + "\n---\n".join(url_contexts)

        msg = IncomingMessage(
            text=text,
            sender_id=str(update.message.from_user.id),
            timestamp=datetime.now(),
            raw=update,
            channel="telegram",
        )

        if self._handler:
            await self._handler(msg)

    async def _handle_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Sprachnachricht → Whisper-Transkription → Mantis-Handler."""
        if not update.message:
            return
        if not self._authorized(update):
            return
        self._chat_id = str(update.message.chat_id)

        voice = update.message.voice or update.message.audio
        if not voice:
            return

        await self._app.bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)

        try:
            tg_file = await context.bot.get_file(voice.file_id)
            suffix = ".ogg" if update.message.voice else ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)
        except Exception as e:
            log.error(f"Sprachnachricht-Download fehlgeschlagen: {e}")
            await self.send("⚠️ Konnte Sprachnachricht nicht herunterladen.")
            return

        try:
            text = await _transcribe(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if not text:
            await self.send("🎤 Konnte die Sprachnachricht nicht verstehen. Bitte schreib es kurz.")
            return

        log.info(f"🎤 Transkription: {text[:100]}")
        await self.send(f"🎤 _{text}_")

        msg = IncomingMessage(
            text=text,
            sender_id=str(update.message.from_user.id),
            timestamp=datetime.now(),
            raw=update,
            channel="telegram",
        )
        if self._handler:
            await self._handler(msg)

    async def _handle_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Foto → Claude Haiku Vision → Mahlzeit-Logging oder Bildbeschreibung."""
        if not update.message or not update.message.photo:
            return
        if not self._authorized(update):
            return
        self._chat_id = str(update.message.chat_id)

        await self._app.bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)

        # Höchste Auflösung nehmen
        photo = sorted(update.message.photo, key=lambda p: p.file_size or 0)[-1]
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)
            image_bytes = Path(tmp_path).read_bytes()
        except Exception as e:
            log.error(f"Foto-Download fehlgeschlagen: {e}")
            await self.send("⚠️ Konnte das Foto nicht herunterladen.")
            return
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        caption = update.message.caption or ""
        description = await _describe_image(image_bytes)

        if description.startswith("🍽️ MAHLZEIT:"):
            # Mahlzeit erkannt → als Tracking-Befehl an Mantis weiterleiten
            meal_text = (
                f"{description}\n\n"
                f"Bitte diese Mahlzeit in meinem Journal tracken."
                + (f" Zusatz: {caption}" if caption else "")
            )
            await self.send(description)
            msg = IncomingMessage(
                text=meal_text,
                sender_id=str(update.message.from_user.id),
                timestamp=datetime.now(),
                raw=update,
                channel="telegram",
            )
            if self._handler:
                await self._handler(msg)
        else:
            # Allgemeines Bild → beschreiben und ggf. auf Caption antworten
            response = description
            if caption:
                msg = IncomingMessage(
                    text=f"{description}\n\nFrage des Nutzers zum Bild: {caption}",
                    sender_id=str(update.message.from_user.id),
                    timestamp=datetime.now(),
                    raw=update,
                    channel="telegram",
                )
                await self.send(description)
                if self._handler:
                    await self._handler(msg)
            else:
                await self.send(response)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Inline-Keyboard Button-Klicks verarbeiten.

        Callback-Daten-Format: "<action>:<payload>"
        Bekannte Aktionen:
          task_done:<id>       — Task als erledigt markieren
          task_skip:<id>       — Task überspringen / zurückstellen
          habit_done:<id>      — Habit für heute abhaken
          habit_skip:<id>      — Habit heute überspringen
          confirm:<free_text>  — Freitext-Bestätigung an Mantis weiterleiten
        """
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()  # Lade-Spinner im Button entfernen

        data: str = query.data
        action, _, payload = data.partition(":")

        try:
            if action == "task_done":
                from domains import tasks as t_d
                t_d.complete_task(int(payload))
                await query.edit_message_text(f"✅ Task #{payload} erledigt.")

            elif action == "task_skip":
                from domains import tasks as t_d
                # "todo" = zurück in die offene Liste; "open" ist KEIN gültiger
                # Status (nur ein Filter-Alias) und würde den Task unsichtbar machen.
                t_d.set_status(int(payload), "todo")
                await query.edit_message_text(f"⏭ Task #{payload} zurückgestellt.")

            elif action == "habit_done":
                from domains import habits as h_d
                from datetime import date
                h_d.log_habit(int(payload), on_date=date.today(), done=True)
                await query.edit_message_text(f"✅ Gewohnheit #{payload} abgehakt.")

            elif action == "habit_skip":
                from domains import habits as h_d
                from datetime import date
                h_d.log_habit(int(payload), on_date=date.today(), done=False)
                await query.edit_message_text(f"⏭ Gewohnheit #{payload} übersprungen.")

            elif action == "confirm":
                # Freitext als normale Nachricht an Mantis weiterleiten
                if self._handler:
                    from communication.base import IncomingMessage
                    msg = IncomingMessage(
                        text=payload,
                        sender_id=str(query.from_user.id) if query.from_user else "",
                        timestamp=datetime.now(),
                        raw=update,
                        channel="telegram",
                    )
                    await query.edit_message_reply_markup(reply_markup=None)
                    await self._handler(msg)
                else:
                    await query.edit_message_text(f"↩ {payload}")

            else:
                log.warning(f"Unbekannte Callback-Aktion: '{action}'")
                await query.edit_message_text("⚠️ Unbekannte Aktion.")

        except Exception as e:
            log.error(f"Callback-Handler Fehler ({data}): {e}")
            try:
                await query.edit_message_text(f"⚠️ Fehler: {e}")
            except Exception:
                pass

    async def _handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._authorized(update):
            log.warning("/start von nicht autorisierter ID verworfen")
            return
        self._chat_id = str(update.message.chat_id)
        await update.message.reply_text(
            "Mantis online. Was brauchst du?"
        )

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text("✅ System läuft.")

    async def start(self) -> None:
        if not self._token:
            raise ValueError("TELEGRAM_BOT_TOKEN nicht gesetzt in .env")

        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )

        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self._app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._handle_voice)
        )
        self._app.add_handler(
            MessageHandler(filters.PHOTO, self._handle_photo)
        )
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        print("📱 Telegram Bot gestartet – warte auf Nachrichten...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
