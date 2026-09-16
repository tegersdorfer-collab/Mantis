"""Der Forge-Telegram-Bot (Nachtrag 3a): Tasks einreihen, Stand abfragen,
wartende Tasks verwerfen, geparkte neu einreihen, den Daemon anhalten.

Kein LLM. Freitext wird nicht interpretiert, sondern wörtlich als Task-Titel
übernommen; Knöpfe tragen nur Integer-IDs. Es gibt keinen Weg von Telegram
in einen Shell-Aufruf. Merge-Knöpfe kommen mit 3b — die Freigabe bleibt beim
CLI mit Diff-Lesen im Worktree.

Die Befehlslogik sind reine Funktionen (antwort_auf, knopf_gedrueckt), die
Telegram-Handler darunter sind Dreizeiler. Tests treffen die Funktionen mit
gepatchter queue/freigabe — kein Telegram aus Tests.
"""
import logging
import os
import re
import sys
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from core import db

from forge import bericht, daemon, freigabe, journal, melden, queue
from forge import models as m

log = logging.getLogger(__name__)

HILFE = "Kenn ich nicht. Freitext = Task, /status /queue /requeue <id> /stop"
TITEL_MAX = 200
_KNOPF = re.compile(r"^(verwerfen|requeue):(\d+)$")


@dataclass
class Antwort:
    """Text plus Inline-Knöpfe: Zeilen aus (Beschriftung, callback_data) —
    dasselbe Format wie TelegramChannel.send_with_buttons."""
    text: str
    knoepfe: list[list[tuple[str, str]]] = field(default_factory=list)


def antwort_auf(text: str) -> Antwort:
    """Eine Nachricht von Timo → eine Antwort. Befehle beginnen mit `/`,
    alles andere ist ein neuer Task."""
    text = text.strip()
    if not text:
        return Antwort(HILFE)
    if text.startswith("/"):
        befehl, _, rest = text[1:].partition(" ")
        befehl = befehl.split("@", 1)[0].lower()  # /stop@AIMantisBot in Gruppen
        if befehl == "status":
            return _status()
        if befehl == "queue":
            return _queue()
        if befehl == "requeue":
            return _requeue(rest.strip())
        if befehl == "stop":
            return Antwort(freigabe.stoppen())
        return Antwort(HILFE)
    return _einreihen(text)


def knopf_gedrueckt(daten: str) -> Antwort:
    """Callback eines Inline-Knopfs. Bekannt: `verwerfen:<id>`, `requeue:<id>`."""
    treffer = _KNOPF.match(daten or "")
    if not treffer:
        log.warning("Forge-Bot: unbekannte Callback-Daten verworfen")
        return Antwort("Unbekannter Knopf.")
    aktion, task_id = treffer.group(1), int(treffer.group(2))
    if aktion == "requeue":
        return _requeue(str(task_id))
    return _verwerfen(task_id)


def _einreihen(text: str) -> Antwort:
    titel, _, beschreibung = text.partition("\n")
    titel = titel.strip()[:TITEL_MAX]
    task_id = queue.enqueue(title=titel, description=beschreibung.strip(), source="timo")
    journal.log(task_id, "idea_added", "Eingereiht via Telegram")
    return Antwort(f"#{task_id} eingereiht: {titel}", [[("Verwerfen", f"verwerfen:{task_id}")]])


def _status() -> Antwort:
    if freigabe.daemon_laeuft():
        aktiv = queue.active()
        if aktiv:
            seit = aktiv["updated_at"].strftime("%H:%M") if hasattr(aktiv["updated_at"], "strftime") else aktiv["updated_at"]
            kopf = f"Daemon läuft, #{aktiv['id']} in {aktiv['state']} seit {seit}"
        else:
            kopf = "Daemon läuft, kein Task aktiv"
    else:
        kopf = "Daemon läuft nicht"
    return Antwort(f"{kopf}\n\n{bericht.morgenbericht().rstrip()}")


def _queue() -> Antwort:
    wartend = queue.nach_zustand(m.QUEUED)
    if not wartend:
        return Antwort("Queue leer.")
    zeilen = [f"Wartend: {len(wartend)}"] + [f"#{t['id']} {t['title']}" for t in wartend]
    knoepfe = [[(f"Verwerfen #{t['id']}", f"verwerfen:{t['id']}")] for t in wartend]
    return Antwort("\n".join(zeilen), knoepfe)


def _requeue(rest: str) -> Antwort:
    if not rest.isdigit():
        return Antwort("Nutzung: /requeue <id>")
    task_id = int(rest)
    if freigabe.neu_einreihen(task_id, quelle="Telegram"):
        return Antwort(f"#{task_id} neu eingereiht")
    return Antwort(f"#{task_id}: nicht geparkt/gescheitert")


def _verwerfen(task_id: int) -> Antwort:
    task = queue.hole(task_id)
    if task is None:
        return Antwort(f"#{task_id} gibt es nicht")
    if task["state"] != m.QUEUED:
        return Antwort(f"#{task_id} ist nicht mehr wartend ({task['state']}) — nichts getan")
    # park() ist Compare-and-Swap: hat der Daemon den Task zwischen hole()
    # und hier geclaimt, kommt False — dann nichts tun statt raten.
    if not queue.park(task_id, m.QUEUED, "verworfen via Telegram"):
        return Antwort(f"#{task_id} ist inzwischen aktiv — nichts getan")
    journal.log(task_id, "parked", "Verworfen via Telegram")
    return Antwort(f"#{task_id} verworfen (geparkt)", [[("Zurückholen", f"requeue:{task_id}")]])


# ---------------------------------------------------------------------------
# Telegram-Anbindung. Alles unterhalb ist dünn: Allowlist prüfen, reine
# Funktion rufen, Antwort schicken.
# ---------------------------------------------------------------------------

def _allowlist() -> set[str]:
    """TELEGRAM_CHAT_ID plus TELEGRAM_ALLOWED_IDS (kommagetrennt) aus der
    Umgebung. Die Chat-ID eines Privatchats ist die User-ID — sie gilt für
    den Mantis-Bot und diesen Bot gleichermaßen."""
    ids = {os.environ.get("TELEGRAM_CHAT_ID", "").strip()}
    ids |= {s.strip() for s in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",")}
    return {i for i in ids if i}


def _absender_ok(user_id: str, chat_id: str, erlaubt: set[str]) -> bool:
    """Strikt: ohne Liste niemand. Kein Trust-on-first-use wie in
    TelegramChannel — der Bot reiht Aufgaben ein, die Agenten im Repo
    ausführen."""
    return bool(erlaubt) and (user_id in erlaubt or chat_id in erlaubt)


def _markup(antwort: Antwort) -> InlineKeyboardMarkup | None:
    if not antwort.knoepfe:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=daten) for label, daten in zeile]
         for zeile in antwort.knoepfe]
    )


# Die Handler rufen synchrone DB-/pgrep-Aufrufe (queue, freigabe) und
# blockieren die Event-Loop damit kurz — für einen Ein-Personen-Bot in
# Ordnung, aber nichts Langsames hier reinhängen.

async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nachricht = update.message
    if nachricht is None or nachricht.from_user is None:
        return
    if not _absender_ok(str(nachricht.from_user.id), str(nachricht.chat_id), context.bot_data["erlaubt"]):
        log.warning(f"Forge-Bot: Nachricht von nicht erlaubter ID {nachricht.from_user.id} ignoriert")
        return
    antwort = antwort_auf(nachricht.text or "")
    stuecke = melden.teile(antwort.text)
    if not stuecke:
        # Telegram lehnt leeren Text ab — kann bei antwort_auf("") nicht
        # vorkommen (liefert HILFE), aber sicher ist sicher.
        return
    for i, stueck in enumerate(stuecke):
        # Knöpfe hängen am letzten Stück, damit sie unter dem Text stehen.
        await nachricht.reply_text(stueck, reply_markup=_markup(antwort) if i == len(stuecke) - 1 else None)


async def _on_knopf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    chat_id = str(query.message.chat.id) if query.message else ""
    if not _absender_ok(str(query.from_user.id), chat_id, context.bot_data["erlaubt"]):
        log.warning(f"Forge-Bot: Knopf von nicht erlaubter ID {query.from_user.id} ignoriert")
        return
    await query.answer()
    antwort = knopf_gedrueckt(query.data or "")
    text = antwort.text[:melden.TELEGRAM_MAX]
    # query.message ist bei einer alten Nachricht ein InaccessibleMessage
    # (PTB 22) — das kann nicht editiert werden, dann stattdessen neu senden.
    if isinstance(query.message, Message):
        # Ersetzt die Nachricht mit dem Knopf durch das Ergebnis — z.B. beim
        # Verwerfen durch den [Zurückholen]-Knopf, sonst (wie früher) ohne
        # Knöpfe.
        await query.edit_message_text(text, reply_markup=_markup(antwort))
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=_markup(antwort))


async def _on_fehler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB-Fehlerhandler: sonst Traceback ohne Antwort — Timo sähe nur
    Stille statt eines Hinweises, dass etwas schiefging."""
    log.exception("Forge-Bot: Handler-Fehler")
    if isinstance(update, Update) and update.effective_message:
        # Nie context.error selbst in die Antwort — die Ausnahme könnte
        # Interna oder (bei einem Netzwerkfehler) sogar die Token-URL tragen.
        await update.effective_message.reply_text("Fehler — siehe /tmp/mantis_forge_bot_err.log")


def _polling_starten(token: str, erlaubt: set[str]) -> None:
    """Blockiert bis SIGTERM. Eigene Funktion, damit main() ohne Telegram
    testbar ist."""
    app = Application.builder().token(token).build()
    app.bot_data["erlaubt"] = erlaubt
    # filters.TEXT umfasst Befehle (/status …); antwort_auf() unterscheidet.
    # Voice, Fotos, Dokumente haben keinen Handler und bleiben unbeantwortet.
    app.add_handler(MessageHandler(filters.TEXT, _on_text))
    app.add_handler(CallbackQueryHandler(_on_knopf))
    app.add_error_handler(_on_fehler)
    log.info("Forge-Bot: Polling gestartet")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


def main() -> int:
    """launchd-Einstieg (com.mantis.forge-bot). Exit 2 ohne Token oder ohne
    Allowlist, Exit 3 wenn die Datenbank nicht erreichbar ist — launchd
    wartet dank KeepAlive+ThrottleInterval 60 s, dann der nächste Versuch,
    und die Meldung steht im Log."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    # httpx (von PTB für die Bot-API genutzt) loggt auf INFO die volle
    # Request-URL inklusive Token (".../bot<TOKEN>/getUpdates") — bei jedem
    # Poll, alle paar Sekunden, geradewegs in StandardOutPath. WARNING
    # unterdrückt das, ohne echte Fehler zu verschlucken.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    daemon.lade_api_schluessel(daemon.API_SCHLUESSEL_DATEI)
    daemon.lade_api_schluessel(daemon.ENV_DATEI, nur=daemon.ENV_NUR)
    token = os.environ.get("FORGE_BOT_TOKEN", "").strip()
    if not token:
        log.error(f"Forge-Bot: FORGE_BOT_TOKEN fehlt in {daemon.API_SCHLUESSEL_DATEI}")
        return 2
    erlaubt = _allowlist()
    if not erlaubt:
        log.error(f"Forge-Bot: TELEGRAM_CHAT_ID/TELEGRAM_ALLOWED_IDS fehlen in {daemon.ENV_DATEI} — "
                  "kein Trust-on-first-use, der Bot startet nicht")
        return 2
    try:
        db.init_pool()
    except Exception as exc:
        log.error(f"Forge-Bot: Datenbank nicht erreichbar ({type(exc).__name__}) — "
                  "launchd versucht es in 60 s erneut")
        return 3
    log.info(f"Forge-Bot: {len(erlaubt)} erlaubte ID(s)")
    _polling_starten(token, erlaubt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
