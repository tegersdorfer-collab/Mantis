"""Robuste Zeit-/Datum-Parser für Tool-Argumente vom LLM."""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config

_TZ = ZoneInfo(getattr(config, "OWNER_TIMEZONE", "Europe/Berlin"))

_WEEKDAYS_DE = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
    "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
}


# Füllwörter vor einem Wochentag ("nächsten freitag")
_FILLER_PREFIX = {"nächsten", "naechsten", "kommenden", "diesen", "am", "den", "nächste", "naechste"}

# Tageszeit-Angaben. Stehen sie allein, meinen sie heute; nach einem Datum
# sind sie nur Beiwerk ("morgen früh 8:00").
_TIME_OF_DAY = {
    "früh", "frueh", "morgens", "vormittags", "mittags",
    "nachmittags", "abends", "abend", "nachts", "nacht", "morgen",
}

_NOW_WORDS = {"jetzt", "now", "sofort", "gleich"}


def _parse_time(s: str) -> tuple[int, int] | None:
    """Erkennt 14:30 / 14.30 / 14 uhr 30 / 14:30 uhr / 9 uhr / 14.
    Gibt None zurück, wenn nichts Gültiges drinsteht — nie einen Default."""
    s = s.strip().lower().strip(",")
    if not s:
        return None
    # "14 uhr 30" → "14:30", "14:30 uhr" → "14:30"
    m = re.fullmatch(r"(\d{1,2})\s*uhr\s*(\d{1,2})", s)
    if m:
        s = f"{m.group(1)}:{m.group(2)}"
    else:
        s = re.sub(r"\s*uhr\s*$", "", s).strip()
    m = re.fullmatch(r"(\d{1,2})\s*[:.]\s*(\d{1,2})", s) or re.fullmatch(r"(\d{1,2})", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _next_weekday(now: datetime, wd: int) -> datetime:
    """Gibt nächsten Wochentag >= morgen zurück."""
    days_ahead = wd - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return now + timedelta(days=days_ahead)


def parse_datetime(s: str) -> datetime | None:
    """Parst eine Zeitangabe und gibt sie tz-aware (config.OWNER_TIMEZONE) zurück,
    damit naive datetimes nicht implizit von der Postgres-Session-Timezone abhängen."""
    dt = _parse_naive(s)
    return dt.replace(tzinfo=_TZ) if dt else None


def _parse_naive(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().lower()
    # datetime-local format (from HTML input)
    if "t" in s and len(s) >= 16:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
    now = datetime.now()

    # Relative Schlüsselwörter
    if s in _NOW_WORDS:
        return now

    if s.startswith("in "):
        try:
            parts = s.split()
            n = int(parts[1])
            unit = parts[2]
            if unit.startswith("min"):
                return now + timedelta(minutes=n)
            if unit.startswith("std") or unit.startswith("hour") or unit.startswith("stund"):
                return now + timedelta(hours=n)
            if unit.startswith("tag") or unit.startswith("day"):
                return now + timedelta(days=n)
            if unit.startswith("woch") or unit.startswith("week"):
                return now + timedelta(weeks=n)
        except Exception:
            pass

    tokens = s.split()
    base = None
    rest: list[str] = []

    # Relative Schlüsselwörter: nur als ganzes Token, damit "morgens" nicht
    # als "morgen" und "sofort" nicht als "so"(nntag) durchgeht.
    if tokens and tokens[0] in ("übermorgen", "uebermorgen"):
        base, rest = now + timedelta(days=2), tokens[1:]
    elif tokens and tokens[0] == "morgen":
        base, rest = now + timedelta(days=1), tokens[1:]
    elif tokens and tokens[0] == "heute":
        base, rest = now, tokens[1:]
    else:
        clean = [t for t in tokens if t not in _FILLER_PREFIX]
        if clean and clean[0] in _WEEKDAYS_DE:
            base, rest = _next_weekday(now, _WEEKDAYS_DE[clean[0]]), clean[1:]
        elif clean and clean[0] in _TIME_OF_DAY:
            base, rest = now, clean

    if base is not None:
        rest = [t for t in rest if t not in _TIME_OF_DAY and t not in ("um", ",")]
        remainder = " ".join(rest).strip()
        if not remainder:
            return base.replace(hour=9, minute=0, second=0, microsecond=0)
        hm = _parse_time(remainder)
        if hm is None:
            return None  # lieber nichts als stillschweigend 9:00
        return base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)

    # Absolute Formate mit Jahr
    for fmt in (
        "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M",
        "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
        "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    # Formate ohne Jahr: aktuelles Jahr explizit anhängen statt strptime-Default
    # (1900-Default entfällt ab Python 3.15, gh-70647); Vergangenheit → nächstes Jahr
    for fmt in ("%d.%m. %H:%M", "%d.%m."):
        try:
            dt = datetime.strptime(f"{s} {now.year}", f"{fmt} %Y")
        except ValueError:
            continue
        if dt.date() < now.date():
            try:
                dt = dt.replace(year=now.year + 1)
            except ValueError:  # 29.02., aber Folgejahr ist kein Schaltjahr
                return None
        return dt

    # Nur Uhrzeit → heute
    hm = _parse_time(s)
    if hm is not None:
        return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return None


def parse_date(s: str) -> date | None:
    dt = parse_datetime(s)
    return dt.date() if dt else None
