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
