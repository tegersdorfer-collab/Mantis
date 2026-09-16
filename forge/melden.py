"""Eine Telegram-Nachricht ohne Bot-Instanz (Nachtrag 3a).

Der Daemon schickt damit den Morgenbericht und die Not-Aus-Meldung. Ein
synchroner POST auf `sendMessage` reicht: kein Polling, kein zweiter Prozess,
keine Abhängigkeit vom Bot (forge/bot.py), der tagsüber läuft. Diese Funktion
wirft nie — die Nacht darf nicht an Telegram scheitern, auch nicht bei einer
abgerissenen Antwort oder einer sonst unerwarteten Ausnahme (dafür fängt ein
Catch-all den Rest ab). Sie loggt bei Fehlern den HTTP-Status oder den
Ausnahme-Typnamen, nie die URL oder `str(exc)` (beide könnten den Token
enthalten).
"""
import http.client
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# Telegram lehnt Nachrichten über 4096 Zeichen ab (Bot API, sendMessage).
TELEGRAM_MAX = 4096
TIMEOUT_SEKUNDEN = 15
_API = "https://api.telegram.org/bot{token}/sendMessage"


def teile(text: str, max_len: int = TELEGRAM_MAX) -> list[str]:
    """Zerlegt den Text in Stücke bis `max_len`, bevorzugt am letzten
    Zeilenumbruch vor der Grenze. Leerer Text ergibt keine Stücke."""
    stuecke: list[str] = []
    rest = text
    while len(rest) > max_len:
        schnitt = rest.rfind("\n", 0, max_len)
        if schnitt <= 0:
            schnitt = max_len
        stuecke.append(rest[:schnitt])
        rest = rest[schnitt:].lstrip("\n")
    if rest:
        stuecke.append(rest)
    return stuecke


def sende(text: str) -> bool:
    """Schickt `text` an TELEGRAM_CHAT_ID über den Forge-Bot (FORGE_BOT_TOKEN).

    True, wenn alle Stücke angekommen sind. False — und nur eine Log-Zeile —
    wenn Token oder Chat-ID fehlen, das Netz weg ist, Telegram einen
    HTTP-Fehler oder `ok: false` liefert."""
    token = os.environ.get("FORGE_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("Telegram-Meldung übersprungen: FORGE_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt")
        return False
    url = _API.format(token=token)
    for stueck in teile(text):
        daten = urllib.parse.urlencode({"chat_id": chat_id, "text": stueck}).encode()
        anfrage = urllib.request.Request(url, data=daten, method="POST")
        try:
            with urllib.request.urlopen(anfrage, timeout=TIMEOUT_SEKUNDEN) as antwort:
                koerper = json.loads(antwort.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            # exc.code, nie str(exc) oder exc.url — die URL trägt den Token.
            log.warning(f"Telegram-Meldung fehlgeschlagen: HTTP {exc.code}")
            return False
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            log.warning(f"Telegram nicht erreichbar: {getattr(exc, 'reason', exc)}")
            return False
        except Exception as exc:
            # Catch-all: nie str(exc) — der Token könnte darin stecken, nur der Typname.
            log.warning(f"Telegram-Meldung fehlgeschlagen: {type(exc).__name__}")
            return False
        if not isinstance(koerper, dict):
            log.warning("Telegram lehnt ab: unerwartete Antwort")
            return False
        if not koerper.get("ok"):
            log.warning(f"Telegram lehnt ab: {koerper.get('description', 'ohne Grund')}")
            return False
    return True
