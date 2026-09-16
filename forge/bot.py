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
import re
from dataclasses import dataclass, field

from forge import bericht, freigabe, journal, queue
from forge import models as m

log = logging.getLogger(__name__)

HILFE = "Kenn ich nicht. Freitext = Task, /status /queue /requeue <id> /stop"
TITEL_MAX = 200
_KNOPF_VERWERFEN = re.compile(r"^verwerfen:(\d+)$")


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
    """Callback eines Inline-Knopfs. Nur `verwerfen:<id>` ist bekannt."""
    treffer = _KNOPF_VERWERFEN.match(daten or "")
    if not treffer:
        log.warning("Forge-Bot: unbekannte Callback-Daten verworfen")
        return Antwort("Unbekannter Knopf.")
    return _verwerfen(int(treffer.group(1)))


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
    if freigabe.neu_einreihen(task_id):
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
    return Antwort(f"#{task_id} verworfen (geparkt, /requeue {task_id} holt ihn zurück)")
