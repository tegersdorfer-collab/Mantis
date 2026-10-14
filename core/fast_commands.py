"""Deterministische Fast-Path-Befehle — kritische Fixbefehle ohne LLM.

Der Agent ruft unter dem vollen System-Prompt bei einigen Tool-Befehlen
manchmal gar kein Tool auf, sondern täuscht Erfolg vor ("Lampe an ✓", ohne zu
schalten). Für die wenigen kritischen Fixbefehle (Licht, Roboter-Not-Stopp) ist das
inakzeptabel. Diese Schicht erkennt sie regelbasiert und ruft das Tool DIREKT —
100% zuverlässig, sofort, modellunabhängig.

Designprinzip: **Fehlalarme (Konversation kapern) sind schlimmer als Auslassungen**
(dann übernimmt eben der Agent). Die Regeln sind daher bewusst eng: Fragezeichen →
niemals Fast-Path; Lampe braucht ein Kommando-Verb oder eine sehr kurze Äußerung.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config


@dataclass
class FastCommand:
    tool: str
    args: dict
    label: str  # fürs Logging


_LAMP = {"lampe", "licht", "schreibtischlampe"}
_ON = {"an", "ein", "anmachen", "einschalten", "anschalten"}
_OFF = {"aus", "ausmachen", "ausschalten", "abschalten"}
# Reihenfolge-relevant: Farbe/Helligkeit vor an/aus prüfen.
_COLORS = {"rot": "rot", "grün": "gruen", "gruen": "gruen", "blau": "blau",
           "weiß": "weiss", "weiss": "weiss", "warmweiß": "warmweiss", "warmweiss": "warmweiss"}
_BRIGHT = {"heller": "heller", "dunkler": "dunkler"}
_CMD_VERBS = {"mach", "macht", "mache", "schalt", "schalte", "schaltet",
              "dreh", "drehe", "stell", "stelle", "setz", "setze"}

_FAN = {"ventilator", "lüfter", "luefter", "fan"}
_FAN_UP = {"stärker", "staerker", "schneller", "höher", "hoch"}
_FAN_DOWN = {"schwächer", "schwaecher", "langsamer", "niedriger", "runter"}

_EMAIL = {"mail", "mails", "email", "emails", "postfach", "posteingang"}
_EMAIL_NEW = {"neue", "neuen", "ungelesene", "ungelesen", "neu"}

_ROBOT = {"roboter", "robot", "droide", "droid", "x5"}
_STOP = {"stopp", "stop", "stoppen", "halt", "anhalten"}
_HARD_STOP = {"notaus", "notstopp", "nothalt", "notstop"}

_MUSIC = {"musik", "spotify", "lied", "song"}
_MUSIC_NEXT = {"nächstes", "nächster", "nächste", "skip", "next", "überspringen", "überspring"}
_MUSIC_PREV = {"vorheriges", "vorheriger", "vorherige", "zurück", "previous"}
_MUSIC_PAUSE = {"pause", "pausiere", "pausieren", "stopp", "stop", "aus", "ausmachen"}
_MUSIC_PLAY = {"an", "play", "weiter", "abspielen", "spiel", "spiele", "anmachen"}
_MUSIC_STATUS = {"läuft", "spielt"}  # Wiedergabe-Status
_HEALTH_STATUS = {"recovery", "gesundheitszustand", "gesundheitlich", "regeneration",
                  "erholung", "health"}
_HEALTH_ASK = {"wie", "status"}
_SKILLTREE = {"skilltree", "level", "quest", "quests"}

_UIAUTO_VERBS = {"bediene", "bedien", "steuere", "steuer"}
# Alle reinen Steuerwörter zusammen — dient der Abgrenzung „spiel [X]" (Suche)
# von „spiel weiter"/„spiel musik" (Steuerung).
_MUSIC_CONTROL = _MUSIC | _MUSIC_NEXT | _MUSIC_PREV | _MUSIC_PAUSE | _MUSIC_PLAY


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def match(text: str) -> FastCommand | None:
    """Gibt einen FastCommand zurück, wenn die Äußerung ein eindeutiger Fixbefehl
    ist — sonst None (dann übernimmt der Agent)."""
    w = _words(text)
    if not w:
        return None

    # ── Musik-Status ("was läuft?") ───────────────────────────────────────────
    # Inhärent eine Frage → VOR dem Fragezeichen-Guard prüfen. Deterministisch,
    # weil das kleine Modell hier sonst gern Erfolg vortäuscht ("läuft Musik")
    # statt den echten Track abzufragen.
    if (w & _MUSIC_STATUS) and ("was" in w or "welches" in w or "welcher" in w or w & _MUSIC):
        return FastCommand("spotify", {"action": "status"}, "musik-status")

    # ── Email-Lese-Abfrage ("neue mails?") ────────────────────────────────────
    # Ebenfalls inhärent eine Frage → vor dem Fragezeichen-Guard. Nur die Lese-
    # Absicht (unread/Posteingang); Senden/Suchen brauchen Parameter → Agent.
    if (w & _EMAIL) and (w & _EMAIL_NEW or "posteingang" in w):
        return FastCommand("email_unread", {}, "email-unread")

    # ── Health-Status ("wie ist meine recovery?") ─────────────────────────────
    # Inhärent eine Frage → vor dem Fragezeichen-Guard. Frage-Rahmen (wie/status/?)
    # nötig, damit "spiel recovery von X" (Musik) nicht gekapert wird. Deterministisch,
    # weil das kleine Modell sonst gern Gesundheitswerte frei erfindet.
    if (w & _HEALTH_STATUS) and (w & _HEALTH_ASK or "?" in text):
        return FastCommand("get_health_scores", {}, "health-scores")

    # ── Skilltree-Status ("wie ist mein level?") ──────────────────────────────
    # Inhärent eine Frage → vor dem Fragezeichen-Guard. Frage-Rahmen (wie/status/?)
    # nötig, damit "spiel level up" (Musik) nicht gekapert wird.
    if (w & _SKILLTREE) and (w & _HEALTH_ASK or "?" in text):
        return FastCommand("get_skilltree", {}, "skilltree")

    if "?" in text:
        return None  # Fragen sind keine Befehle

    # ── Roboter-Not-Stopp (Sicherheit) ───────────────────────────────────────
    if (w & _HARD_STOP) or (w & _ROBOT and w & _STOP):
        return FastCommand("robot_control", {"action": "stopp"}, "robot-stopp")

    # ── UI-Automatik-Entry ────────────────────────────────────────────────────
    # Die Tool-Auswahl wählte computer_task unzuverlässig (griff im Test zu create_skill).
    # Enger Fast-Path: explizites „bediene/steuere … app …" → computer_task mit
    # der ganzen Äußerung als Ziel (der Background-Agent parst App + Aufgabe daraus).
    if (w & _UIAUTO_VERBS) and "app" in w:
        return FastCommand("computer_task", {"goal": text.strip()}, "computer-task")

    # ── Musik (Spotify) ───────────────────────────────────────────────────────
    if w & _MUSIC:
        # Nur echte Kommandos: kurz ("musik an") oder Kommando-Verb in über-
        # schaubarer Länge ("mach die musik aus"). "von" signalisiert eine
        # Such-Anfrage ("spiel musik von queen") → Agent mit Web-API-Suche.
        if "von" not in w and (len(w) <= 3 or (w & _CMD_VERBS and len(w) <= 6)):
            # Reihenfolge: next/prev VOR pause/play — "musik weiter" ist play,
            # "nächstes lied" ist next (enthält kein Play-Wort).
            if w & _MUSIC_NEXT:
                return FastCommand("spotify", {"action": "next"}, "musik-next")
            if w & _MUSIC_PREV:
                return FastCommand("spotify", {"action": "previous"}, "musik-previous")
            if w & _MUSIC_PAUSE:
                return FastCommand("spotify", {"action": "pause"}, "musik-pause")
            if w & _MUSIC_PLAY:
                return FastCommand("spotify", {"action": "play"}, "musik-play")
    elif len(w) == 1:
        # Ein-Wort-Befehle sind auch ohne Musik-Kontextwort eindeutig.
        if w & {"pause"}:
            return FastCommand("spotify", {"action": "pause"}, "musik-pause")
        if w & {"skip", "next"}:
            return FastCommand("spotify", {"action": "next"}, "musik-next")

    # ── Ventilator ────────────────────────────────────────────────────────────
    if w & _FAN:
        if w & _CMD_VERBS or len(w) <= 3:
            if w & _FAN_UP:
                return FastCommand("ventilator", {"action": "staerker"}, "ventilator-staerker")
            if w & _FAN_DOWN:
                return FastCommand("ventilator", {"action": "schwaecher"}, "ventilator-schwaecher")
            if w & _OFF:
                return FastCommand("ventilator", {"action": "aus"}, "ventilator-aus")
            if w & _ON:
                return FastCommand("ventilator", {"action": "an"}, "ventilator-an")

    # ── Lampe / Licht ─────────────────────────────────────────────────────────
    if w & _LAMP:
        # Nur echte Kommandos: entweder ein Kommando-Verb oder sehr kurz ("lampe an").
        if not (w & _CMD_VERBS or len(w) <= 3):
            return None
        for token, action in _COLORS.items():
            if token in w:
                return FastCommand("lampe", {"action": action}, f"lampe-{action}")
        for token, action in _BRIGHT.items():
            if token in w:
                return FastCommand("lampe", {"action": action}, f"lampe-{action}")
        if w & _OFF:
            return FastCommand("lampe", {"action": "aus"}, "lampe-aus")
        if w & _ON:
            return FastCommand("lampe", {"action": "an"}, "lampe-an")

    # ── "spiel [X]" — Suche+Abspielen deterministisch ─────────────────────────
    # Auch das ruft das kleine Modell nicht zuverlässig auf (und select_tools kann
    # nicht jeden Songtitel als Keyword kennen). Wir extrahieren den Suchbegriff
    # selbst. Reine Steuerkommandos ("spiel musik", "spiel weiter") sind oben schon
    # abgefangen bzw. werden hier ausgeschlossen (Query nur aus Steuerwörtern).
    m = re.match(r"^\s*spiele?\s+(.+)$", text.strip(), re.IGNORECASE)
    if m:
        query = m.group(1).strip()
        qwords = set(re.findall(r"\w+", query.lower()))
        if query and not (qwords <= _MUSIC_CONTROL):
            args = {"action": "spiel", "query": query}
            if "playlist" in qwords:
                args["typ"] = "playlist"
            return FastCommand("spotify", args, "musik-spiel")

    return None


_WEEKDAYS_DE = (
    "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag",
)
_MONTHS_DE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)
_DURATION_NUMBERS = {
    "ein": 1, "eine": 1, "eins": 1, "einen": 1,
    "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13,
    "vierzehn": 14, "fünfzehn": 15, "fuenfzehn": 15,
    "sechzehn": 16, "siebzehn": 17, "achtzehn": 18,
    "neunzehn": 19, "zwanzig": 20,
}
_DURATION_PREFIX_RE = re.compile(
    r"^(?:(?:stell(?:e)?|setz(?:e)?|setze)\s+(?:mir\s+)?(?:einen|den)?\s*timer\s+"
    r"(?:auf|in|für)\s+|timer\s+(?:auf|in|für)\s+)"
    r"(?P<count>\d{1,3}|[a-zäöü]+)\s*"
    r"(?P<unit>sekunden?|sek|minuten?|min|stunden?|std|h)$",
    re.IGNORECASE,
)
_DURATION_SUFFIX_RE = re.compile(
    r"^(?P<count>\d{1,3}|[a-zäöü]+)\s*(?P<unit>sekunden?|sek|minuten?|min|stunden?|std|h)\s+timer$",
    re.IGNORECASE,
)
_TIMER_STOP_RE = re.compile(
    r"^(?:timer\s+(?:stopp(?:en)?|stop(?:pen)?|abbrechen|beenden)|"
    r"(?:stopp(?:e|en)?|stop(?:pe|pen)?|beende|abbrechen)\s+(?:den\s+)?timer)$",
    re.IGNORECASE,
)
_WAKEWORD_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hey|okay)\s*,?\s+)?"
    r"(?P<wake>mantis|mäntis|mann\s*,?\s*das|man\s*tis|mentis|mantas|manthis)"
    r"(?:\s*,\s*|\s+|$)",
    re.IGNORECASE,
)


def _voice_text(text: str) -> str:
    value = text.strip().lower()
    return re.sub(r"[\s?!.;,]+$", "", value).strip()


def _is_ambiguous_wakeword(wake: str) -> bool:
    return re.sub(r"[\s,]+", "", wake).casefold() == "manndas"


def has_voice_wakeword(text: str) -> bool:
    """Prüft ein führendes Weckwort ohne mehrdeutige Alltagssätze abzufangen."""
    wakeword = _WAKEWORD_PREFIX_RE.match(text)
    if wakeword is None:
        return False
    if not _is_ambiguous_wakeword(wakeword.group("wake")):
        return True
    command = match_voice(text[wakeword.end():])
    return command is not None and command.tool == "voice_reflex"


def _as_voice_reflex(response: str, label: str) -> FastCommand:
    return FastCommand("voice_reflex", {"response": response}, label)


def _duration_seconds(count: str, unit: str) -> int | None:
    if count.isdigit():
        value = int(count)
    else:
        value = _DURATION_NUMBERS.get(count.lower())
    if not value:
        return None
    unit = unit.lower()
    multiplier = 1 if unit.startswith("sek") else 60 if unit.startswith("min") else 3600
    seconds = value * multiplier
    return seconds if seconds <= 86_400 else None


def match_voice(text: str, *, now: datetime | None = None) -> FastCommand | None:
    """Erkennt eng begrenzte Voice-Reflexe und sonstige bestehende Fast-Commands.

    Fragen mit zusätzlichen Orts- oder Sachangaben bleiben beim LLM. `now` ist
    für Tests injizierbar; im Betrieb gilt die konfigurierte lokale Zeitzone.
    """
    if not text or not text.strip():
        return None
    if now is None:
        now = datetime.now(ZoneInfo(config.OWNER_TIMEZONE))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(config.OWNER_TIMEZONE))

    wakeword = _WAKEWORD_PREFIX_RE.match(text)
    if wakeword is not None:
        remainder = text[wakeword.end():]
        if _is_ambiguous_wakeword(wakeword.group("wake")):
            # "Mann, das ..." ist auch gewöhnliche Sprache. Nur als Weckwort werten,
            # wenn der Rest für sich bereits einen eng begrenzten Voice-Reflex trifft.
            command = match_voice(remainder, now=now)
            if command is not None and command.tool == "voice_reflex":
                return command
        else:
            text = remainder

    phrase = _voice_text(text)
    if not phrase:
        return None

    if phrase in {"wie spät ist es", "wie spät ist's", "wie viel uhr ist es", "uhrzeit"}:
        return _as_voice_reflex(f"Es ist {now:%H:%M} Uhr.", "uhrzeit")
    if phrase in {"welches datum ist heute", "was ist heute für ein datum", "was ist das datum",
                  "heutiges datum", "datum"}:
        return _as_voice_reflex(
            f"Heute ist der {now.day}. {_MONTHS_DE[now.month - 1]} {now.year}.", "datum",
        )
    if phrase in {"welcher wochentag ist heute", "welcher tag ist heute", "wochentag"}:
        return _as_voice_reflex(f"Heute ist {_WEEKDAYS_DE[now.weekday()]}.", "wochentag")

    timer_match = _DURATION_PREFIX_RE.fullmatch(phrase) or _DURATION_SUFFIX_RE.fullmatch(phrase)
    if timer_match:
        seconds = _duration_seconds(timer_match.group("count"), timer_match.group("unit"))
        if seconds is not None:
            due = (now + timedelta(seconds=seconds)).replace(microsecond=0)
            if seconds % 3600 == 0:
                amount, unit = seconds // 3600, "Stunde" if seconds == 3600 else "Stunden"
            elif seconds % 60 == 0:
                amount, unit = seconds // 60, "Minute" if seconds == 60 else "Minuten"
            else:
                amount, unit = seconds, "Sekunde" if seconds == 1 else "Sekunden"
            return FastCommand(
                "set_reminder",
                {"text": f"Timer: {amount} {unit}", "at": due.strftime("%Y-%m-%dT%H:%M:%S"),
                 "kind": "timer"},
                "timer-start",
            )
    if _TIMER_STOP_RE.fullmatch(phrase):
        return FastCommand("stop_timer", {}, "timer-stop")

    volume_match = re.fullmatch(
        r"(?:(?:(?:mach(?:e)?|stell(?:e)?)\s+)?(?:die\s+)?musik\s+)?"
        r"(?P<direction>lauter|leiser)",
        phrase,
    )
    if volume_match:
        direction = "up" if volume_match.group("direction") == "lauter" else "down"
        return FastCommand("spotify", {"action": f"volume_{direction}"}, f"lautstaerke-{direction}")

    command = match(text)
    if command is not None:
        return command
    if phrase in {"stopp", "stop", "abbrechen", "halt"}:
        return _as_voice_reflex("Okay.", "stopp")
    return None
