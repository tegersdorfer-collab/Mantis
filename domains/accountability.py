"""
Accountability & Momentum — v0: Daily Anchor + Der Block.

Zweck: Timo externe Struktur geben, damit er ins Handeln kommt. Zwei Features:

  A) Daily Anchor — morgens EINE Nachricht (Datum + heutige Termine + „Was ist
     das EINE Ding heute?"). Genau eine Priorität pro Tag, nie eine Liste.
  B) Der Block   — EIN Deep-Work-Block/Tag. Start: „Handy weg. Woran: <Prio>".
     Ende: „Fertig? Ein Satz: was ist rausgekommen?". Geloggt: Block ja/nein +
     der Ein-Satz-Output.

Designprinzipien (hart): sekundenkurze Interaktionen, push-to-action (nie
push-to-reflection), Daten statt Pep-Talk, kein offenes Gespräch. Alle Texte
sind STATISCHE Strings — kein LLM. Das ist schneller, deterministisch und lässt
sich nicht in ein Coaching-Gespräch abdriften (genau das will die Spec nicht).

Zwei Einstiegspunkte:
  - tick(send, dashboard): der Scheduler (aus core.idle_loop) ruft das ~minütlich.
    Sendet Anchor / Blockstart / Blockende zur konfigurierten Zeit, idempotent.
  - intercept(text) -> str | None: der Message-Handler ruft das VOR dem Agenten.
    Bucht eine eingehende Antwort auf den offenen Anchor/Block-Prompt und gibt
    die (kurze, finale) Bestätigung zurück; None = keine Accountability-Antwort,
    normal an den Agenten weiterreichen.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta

from core import db
import config

log = logging.getLogger(__name__)

# Zeitfenster (Minuten) nach der Soll-Zeit, in dem eine verpasste Nachricht noch
# nachgeholt wird. Verhindert, dass ein spät gestarteter Server (z.B. abends nach
# Downtime) morgens/mittags fällige Prompts als Spam-Salve nachfeuert.
CATCHUP_MIN = 180

_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
             "Freitag", "Samstag", "Sonntag"]

# Kurze, klare Bestätigungen für den Blockstart ("go").
_AFFIRMATIVE = {
    "go", "los", "los gehts", "los geht's", "losgehts", "start", "starte",
    "bereit", "jo", "ja", "ok", "okay", "k", "👍", "👊", "💪", "yes", "yep",
}
# Negationen für das Blockende ("ist der Block gelaufen?"). Alles andere gilt als
# echter Output → block_done = TRUE.
_NEGATIVE = {
    "nein", "ne", "nö", "nope", "no", "nichts", "gar nichts", "nicht gelaufen",
    "nicht", "ausgefallen", "-", "–", "0", "null", "nix",
}


# ── Zeit-Helfer ───────────────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))


def _due_at(now: datetime, target: datetime) -> bool:
    """True, wenn now im Fenster [target, target+CATCHUP_MIN] liegt."""
    return target <= now <= target + timedelta(minutes=CATCHUP_MIN)


def _german_date(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


# ── Textklassifikation (deterministisch, kein LLM) ────────────────────────────

def _is_affirmative(text: str) -> bool:
    t = text.strip().lower().rstrip("!.")
    return t in _AFFIRMATIVE or t.startswith(("go", "los", "start"))


def _is_negative(text: str) -> bool:
    t = text.strip().lower().rstrip("!.")
    return t in _NEGATIVE or t.startswith(("nein", "nicht ", "gar nicht"))


def _looks_multiple(text: str) -> bool:
    """Erkennt, ob Timo mehrere Dinge statt EINEM genannt hat.

    Bewusst konservativ: nur klare Listen-Signale zählen (mehrere Zeilen,
    Semikolons, 2+ Kommas, Aufzählungsmarker). Ein einzelnes „X und Y" gilt NICHT
    als Liste — das ist oft eine zusammenhängende Aufgabe, und ein fälschliches
    „Eins. Welches?" nervt mehr, als es hilft.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return True
    if len([p for p in text.split(";") if p.strip()]) >= 2:
        return True
    if text.count(",") >= 2:
        return True
    if len(re.findall(r"(?m)^\s*\d+[.)]", text)) >= 2:
        return True
    return False


# ── DB-Zugriff (eine Zeile pro Tag) ───────────────────────────────────────────

def _row(day: date) -> dict | None:
    return db.query_one("SELECT * FROM daily_log WHERE date = %s", (day,))


def _ensure_row(day: date) -> dict:
    db.execute(
        "INSERT INTO daily_log (date) VALUES (%s) ON CONFLICT (date) DO NOTHING",
        (day,),
    )
    return _row(day)


def _set(day: date, **cols) -> None:
    """Setzt Spalten für den Tag. Spaltennamen sind modul-interne Konstanten
    (kein User-Input) → f-string ist hier sicher."""
    _ensure_row(day)
    keys = list(cols.keys())
    assignments = ", ".join(f"{k} = %s" for k in keys)
    params = tuple(cols[k] for k in keys) + (day,)
    db.execute(f"UPDATE daily_log SET {assignments} WHERE date = %s", params)


# ── Nachrichten-Texte (statisch) ──────────────────────────────────────────────

def _anchor_message(day: date, dashboard) -> str:
    lines = [f"📌 {_german_date(day)}"]
    try:
        events = dashboard.get_upcoming_events(days=1)
    except Exception:
        events = []
    todays = []
    for e in events or []:
        e_date = e.start.date() if hasattr(e.start, "date") else e.start
        if e_date != day:
            continue
        if getattr(e, "all_day", False):
            todays.append(f"ganztägig {e.title}")
        else:
            todays.append(f"{e.start.strftime('%H:%M')} {e.title}")
    lines.append("Termine heute: " + ("; ".join(todays) if todays else "keine"))
    lines.append("")
    lines.append("Was ist das EINE Ding heute?")
    return "\n".join(lines)


def _block_start_message(priority: str | None) -> str:
    focus = priority.strip() if priority and priority.strip() else "— (kein Fokus gesetzt)"
    return f"⏳ Block startet. Handy weg.\nWoran: {focus}"


_BLOCK_END_MESSAGE = "Fertig? Ein Satz: was ist rausgekommen?"


# ── Scheduler-Tick ────────────────────────────────────────────────────────────

async def tick(send, dashboard) -> None:
    """Sendet fällige Accountability-Nachrichten. Wird ~minütlich aus dem Idle-Loop
    aufgerufen. `send` ist eine Coroutine send(text, kind=...) — der zentrale
    Sendepfad (Telegram + Web-Push + Persistenz), damit hier nichts dupliziert wird.

    Jede der drei Nachrichten feuert höchstens einmal pro Tag; der Zustand steht in
    daily_log, also übersteht die Idempotenz auch Neustarts.
    """
    if not config.ACCOUNTABILITY_ENABLED:
        return

    now = datetime.now()
    day = now.date()
    row = _ensure_row(day)

    anchor_dt = datetime.combine(day, _parse_hhmm(config.ACCOUNTABILITY_ANCHOR_TIME))
    block_dt = datetime.combine(day, _parse_hhmm(config.ACCOUNTABILITY_BLOCK_START))
    end_dt = block_dt + timedelta(minutes=config.ACCOUNTABILITY_BLOCK_MINUTES)

    # A) Daily Anchor
    if row.get("anchor_sent_at") is None and _due_at(now, anchor_dt):
        await send(_anchor_message(day, dashboard), kind="anchor")
        _set(day, anchor_sent_at=now)
        log.info("📌 Daily Anchor gesendet")
        return  # eine Nachricht pro Tick reicht — Rest kommt im nächsten Tick

    # B) Blockstart
    if row.get("block_started_at") is None and _due_at(now, block_dt):
        await send(_block_start_message(row.get("priority")), kind="block_start")
        _set(day, block_started_at=now)
        log.info("⏳ Blockstart gesendet")
        return

    # B) Blockende — nur wenn der Start heute wirklich gefeuert hat
    if (row.get("block_started_at") is not None
            and row.get("block_end_prompted_at") is None
            and _due_at(now, end_dt)):
        await send(_BLOCK_END_MESSAGE, kind="block_end")
        _set(day, block_end_prompted_at=now)
        log.info("🏁 Blockende-Frage gesendet")
        return


# ── Message-Interception (Antworten verbuchen) ────────────────────────────────

def intercept(text: str) -> str | None:
    """Bucht eine eingehende Nachricht auf einen offenen Accountability-Prompt.

    Rückgabe:
      str  → das war eine Accountability-Antwort; Text ist die kurze Bestätigung,
             die zurückgeschickt wird (Handler reicht sie NICHT an den Agenten weiter).
      None → keine offene Accountability-Frage bzw. keine passende Antwort → normal
             an den Agenten.
    """
    if not config.ACCOUNTABILITY_ENABLED:
        return None
    text = (text or "").strip()
    if not text:
        return None

    day = date.today()
    row = _row(day)
    if not row:
        return None

    # 1) Blockende: die End-Frage wurde gestellt und wartet auf den Ein-Satz-Output.
    #    Greedy — was auch immer jetzt kommt, ist die Antwort.
    if row.get("block_end_prompted_at") is not None and row.get("block_output") is None:
        done = not _is_negative(text)
        _set(day, block_output=text[:1000], block_done=done)
        log.info("🏁 Block-Output verbucht (done=%s)", done)
        return "Notiert."

    # 2) Blockstart: warten auf ein kurzes „go". Nur eindeutige Bestätigungen
    #    abfangen — alles andere darf im Blockfenster normal an Mantis gehen.
    if (row.get("block_started_at") is not None
            and not row.get("block_confirmed")
            and row.get("block_end_prompted_at") is None):
        if _is_affirmative(text):
            _set(day, block_confirmed=True)
            return "Läuft."
        return None

    # 3) Daily Anchor: warten auf die EINE Priorität.
    if row.get("anchor_sent_at") is not None and not row.get("priority"):
        if _looks_multiple(text):
            return "Eins. Welches?"
        prio = text[:500]
        _set(day, priority=prio)
        log.info("📌 Priorität gesetzt: %s", prio[:60])
        return f"Steht. Heute: {prio}"

    return None
