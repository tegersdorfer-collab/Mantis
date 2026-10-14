"""Kompakter, im Idle-Loop aktualisierter Überblick für Chat- und Voice-Prompts."""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import config

log = logging.getLogger(__name__)
MAX_SNAPSHOT_CHARS = 1200
_SOURCE_NAMES = ("calendar", "tasks", "health", "weather", "reminder", "workouts")
_REQUIRED_SOURCES = frozenset(("calendar", "tasks", "health", "reminder", "workouts"))
_FAILED = object()
_snapshot: SituationSnapshot | None = None


@dataclass(frozen=True)
class SituationSnapshot:
    refreshed_at: datetime
    text: str
    incomplete: bool = False


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(config.OWNER_TIMEZONE)
    except Exception:
        return ZoneInfo("Europe/Berlin")


def _local_now(now: datetime | None = None) -> datetime:
    zone = _timezone()
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        return current.replace(tzinfo=zone)
    return current.astimezone(zone)


def _get(value, key: str, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(_timezone()).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo:
                return parsed.astimezone(_timezone()).replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                return datetime.combine(date.fromisoformat(value), time.min)
            except ValueError:
                return None
    return None


def _short(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _date_label(value, today: date) -> str:
    due = _as_datetime(value)
    if not due:
        return "ohne Fälligkeit"
    if due.date() < today:
        return "überfällig"
    if due.date() == today:
        return "heute"
    if due.date() == today.fromordinal(today.toordinal() + 1):
        return "morgen"
    return due.strftime("%d.%m.")


def _format_events(events, day: date, label: str) -> str:
    entries = []
    for event in events or []:
        start = _as_datetime(_get(event, "start"))
        if start is None or start.date() != day:
            continue
        title = _short(_get(event, "title", "Termin"), 64) or "Termin"
        when = "ganztags" if _get(event, "all_day", False) else start.strftime("%H:%M")
        entries.append((start, f"{when} {title}"))
    entries.sort(key=lambda entry: entry[0])
    if not entries:
        return f"Termine {label}: keine"
    shown = [title for _start, title in entries[:5]]
    if len(entries) > len(shown):
        shown.append(f"+{len(entries) - len(shown)} weitere")
    return f"Termine {label}: " + "; ".join(shown)


def _format_tasks(tasks, today: date) -> str:
    rank = {"high": 0, "hoch": 0, "medium": 1, "mittel": 1, "low": 2, "niedrig": 2}
    open_tasks = [
        task for task in (tasks or [])
        if str(_get(task, "status", "todo")).lower() not in {"done", "archived"}
    ]
    open_tasks.sort(key=lambda task: (
        (_as_datetime(_get(task, "due")) or datetime.max).date(),
        rank.get(str(_get(task, "priority", "")).lower(), 3),
        str(_get(task, "title", "")).lower(),
    ))
    if not open_tasks:
        return "Offene Aufgaben: keine"
    lines = []
    for task in open_tasks[:8]:
        title = _short(_get(task, "title", "Aufgabe"), 54) or "Aufgabe"
        priority = str(_get(task, "priority", "")).lower()
        priority_label = {"high": "hoch", "medium": "mittel", "low": "niedrig"}.get(
            priority, priority if priority in {"hoch", "mittel", "niedrig"} else "normal",
        )
        lines.append(f"{title} ({_date_label(_get(task, 'due'), today)}, {priority_label})")
    return "Offene Aufgaben: " + "; ".join(lines)


def _format_hours(value) -> str | None:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours < 0:
        return None
    return f"{hours:.1f}".replace(".", ",") + " h"


def _format_health(records) -> tuple[str | None, bool]:
    rows = sorted(
        records or [],
        key=lambda row: _as_datetime(_get(row, "date")) or datetime.min,
        reverse=True,
    )
    parts = []
    sleep_record = next(
        (row for row in rows if _get(row, "sleep_duration") is not None), None,
    )
    total = deep = None
    if sleep_record:
        total = _format_hours(_get(sleep_record, "sleep_duration"))
        deep = _format_hours(_get(sleep_record, "sleep_deep"))
        if total:
            parts.append(f"Schlaf letzte Nacht {total}")
        if deep:
            parts.append(f"davon tief {deep}")
    vitals = {
        key: next((_get(row, key) for row in rows if _get(row, key) is not None), None)
        for key in ("resting_hr", "hrv")
    }
    if vitals["resting_hr"] is not None:
        parts.append(f"Ruhepuls {vitals['resting_hr']} bpm")
    if vitals["hrv"] is not None:
        parts.append(f"HRV {vitals['hrv']} ms")
    if not parts:
        return "Gesundheit: keine aktuellen Health-Daten", False
    complete = bool(
        total and deep and vitals["resting_hr"] is not None and vitals["hrv"] is not None
    )
    return "Gesundheit: " + "; ".join(parts), complete


def _format_weather(weather, today: date) -> str | None:
    if not isinstance(weather, Mapping) or weather.get("error"):
        return None
    current = weather.get("now") or {}
    forecast = weather.get("forecast") or []
    today_forecast = next(
        (item for item in forecast if str(_get(item, "date", "")) == today.isoformat()),
        forecast[0] if forecast else {},
    )
    details = []
    temperature = _get(current, "temp")
    if temperature is not None:
        details.append(f"{temperature:g} °C")
    description = _get(current, "desc")
    if description:
        details.append(str(description))
    low, high = _get(today_forecast, "min"), _get(today_forecast, "max")
    if low is not None and high is not None:
        details.append(f"heute {_fmt_number(low)}–{_fmt_number(high)} °C")
    if not details:
        return None
    city = _short(weather.get("city") or "", 32)
    return f"Wetter {city}: " + ", ".join(details)


def _fmt_number(value) -> str:
    return str(value).replace(".", ",")


def _format_reminder(reminder) -> str:
    if not reminder:
        return "Nächster Reminder: keiner"
    when = _as_datetime(_get(reminder, "remind_at"))
    label = when.strftime("%d.%m. %H:%M") if when else "Zeit unbekannt"
    title = _short(_get(reminder, "text", "Reminder"), 72)
    return f"Nächster Reminder: {label} {title}".rstrip()


def _format_workout(workouts) -> str:
    entries = list(workouts or [])
    if not entries:
        return "Letztes Training: keines erfasst"
    latest = max(
        entries,
        key=lambda item: _as_datetime(_get(item, "date")) or datetime.min,
    )
    day = _as_datetime(_get(latest, "date"))
    date_text = day.strftime("%d.%m.") + " " if day else ""
    title = _short(_get(latest, "title", "Training"), 72)
    return f"Letztes Training: {date_text}{title}".rstrip()


def build_snapshot(
    *, now: datetime | None = None, calendar_events=None, tasks=None, health_records=None,
    weather=None, reminder=None, workouts=None, failed_sources=(),
) -> SituationSnapshot:
    """Formatiert vorhandene Domain-Daten. Diese Funktion führt selbst kein I/O aus."""
    current = _local_now(now)
    today = current.date()
    failed = set(failed_sources)
    sections = [f"Stand: {('Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag')[current.weekday()]}, "
                f"{current:%d.%m.%Y, %H:%M} Uhr"]

    if "calendar" not in failed:
        sections.extend((
            _format_events(calendar_events, today, "heute"),
            _format_events(calendar_events, today.fromordinal(today.toordinal() + 1), "morgen"),
        ))
    if "tasks" not in failed:
        sections.append(_format_tasks(tasks, today))
    health_complete = True
    if "health" not in failed:
        health_line, health_complete = _format_health(health_records)
        if health_line:
            sections.append(health_line)
    if "weather" not in failed:
        weather_line = _format_weather(weather, today)
        if weather_line:
            sections.append(weather_line)
    if "reminder" not in failed:
        sections.append(_format_reminder(reminder))
    if "workouts" not in failed:
        sections.append(_format_workout(workouts))

    text = "\n".join(sections)
    clipped = len(text) > MAX_SNAPSHOT_CHARS
    if clipped:
        text = text[:MAX_SNAPSHOT_CHARS - 1].rstrip() + "…"
    incomplete = bool(failed & (_REQUIRED_SOURCES | {"weather"})) or not health_complete or clipped
    return SituationSnapshot(refreshed_at=current, text=text, incomplete=incomplete)


def _default_sources(reminders) -> dict[str, Callable | None]:
    from domains import calendar as calendar_domain
    from domains import fitness, health, tasks
    from domains import weather as weather_domain

    return {
        "calendar": lambda: calendar_domain.upcoming(days=2),
        "tasks": lambda: tasks.list_tasks("open", include_sub=False),
        "health": lambda: health.recent(days=3),
        "weather": weather_domain.get_weather,
        "reminder": getattr(reminders, "next_due", None),
        "workouts": lambda: fitness.recent_workouts(limit=1),
    }


async def _read_source(name: str, source) -> object:
    if source is None:
        return _FAILED if name in _REQUIRED_SOURCES else None
    try:
        if inspect.iscoroutinefunction(source):
            result = await asyncio.wait_for(source(), timeout=12)
        else:
            result = await asyncio.wait_for(asyncio.to_thread(source), timeout=12)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=12)
        if isinstance(result, Mapping) and result.get("error"):
            return _FAILED
        return result
    except Exception as exc:
        log.debug("Lagebild-Quelle %s fehlgeschlagen (%s)", name, type(exc).__name__)
        return _FAILED


async def refresh_snapshot(
    reminders=None, *, sources: Mapping[str, Callable | None] | None = None,
    now: datetime | None = None,
) -> SituationSnapshot:
    """Lädt die Quellen im Hintergrund und ersetzt den Snapshot atomar im Speicher."""
    global _snapshot
    source_map = dict(sources) if sources is not None else _default_sources(reminders)
    results = await asyncio.gather(*(
        _read_source(name, source_map.get(name)) for name in _SOURCE_NAMES
    ))
    values = dict(zip(_SOURCE_NAMES, results))
    failed = {name for name, value in values.items() if value is _FAILED}
    values = {name: None if value is _FAILED else value for name, value in values.items()}
    _snapshot = build_snapshot(
        now=now,
        calendar_events=values["calendar"],
        tasks=values["tasks"],
        health_records=values["health"],
        weather=values["weather"],
        reminder=values["reminder"],
        workouts=values["workouts"],
        failed_sources=failed,
    )
    return _snapshot


def prompt_context(*, now: datetime | None = None) -> str:
    """Liefert ausschließlich den im Prozess gespeicherten Snapshot für den Prompt."""
    if not config.SITUATION_ENABLED:
        return ""
    snapshot = _snapshot
    if snapshot is None:
        return (
            "## Lagebild\nNoch kein Hintergrund-Snapshot verfügbar. "
            "Bei Fragen zu diesen Themen bis zum ersten Refresh Tools verwenden."
        )

    current = _local_now(now)
    refreshed = _local_now(snapshot.refreshed_at)
    age = max(0.0, (current - refreshed).total_seconds())
    stale_after = max(60, int(config.SITUATION_REFRESH_S) + 60)
    stale = age > stale_after
    context = (
        "## Lagebild\n" + snapshot.text + "\n"
        "Beantworte Fragen zu den aufgeführten Themen direkt aus diesem Snapshot. "
        "Nutze Tools nur für Details, Änderungen oder wenn der Stand älter/unvollständig ist."
    )
    if stale or snapshot.incomplete:
        context += "\nHinweis: Lagebild ist "
        context += "veraltet" if stale else "unvollständig"
        if stale and snapshot.incomplete:
            context += " und unvollständig"
        context += "; fehlende oder betroffene Angaben mit Tools prüfen."
    return context
