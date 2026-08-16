"""
DataReader – Facade über mantis-native Datenquellen (Health, Kalender).
KEINE Abhängigkeit mehr von der ai-dashboard. Health kommt aus health_data
(gespeist von iCloud Health.json), Kalender live aus ICS + mantis-eigenen Events.
Klassenname/Methoden bleiben kompatibel zu den bestehenden Aufrufstellen.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from domains import health as health_d
from domains import calendar as calendar_d

log = logging.getLogger(__name__)

_WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _de_date(dt: datetime, with_time: bool) -> str:
    today = date.today()
    d = dt.date() if isinstance(dt, datetime) else dt
    if d == today:
        label = "Heute"
    elif d == today + timedelta(days=1):
        label = "Morgen"
    elif d == today + timedelta(days=2):
        label = "Übermorgen"
    else:
        label = f"{_WD[dt.weekday()]} {dt.strftime('%d.%m.')}"
    return label + dt.strftime(" %H:%M") if with_time else label


@dataclass
class HealthSummary:
    date: date
    steps: Optional[int]
    active_calories: Optional[int]
    exercise_minutes: Optional[int]
    sleep_duration: Optional[float]
    sleep_deep: Optional[float]
    resting_hr: Optional[int]
    hrv: Optional[float]
    weight: Optional[float]

    def format(self) -> str:
        parts = [f"📅 {self.date.strftime('%d.%m.%Y')}"]
        if self.steps:           parts.append(f"👟 {self.steps:,} Schritte")
        if self.active_calories: parts.append(f"🔥 {self.active_calories} kcal aktiv")
        if self.exercise_minutes:parts.append(f"⏱ {self.exercise_minutes} Min Sport")
        if self.sleep_duration:  parts.append(f"😴 {self.sleep_duration:.1f}h Schlaf")
        if self.sleep_deep:      parts.append(f"   Tiefschlaf: {self.sleep_deep:.1f}h")
        if self.resting_hr:      parts.append(f"❤️ {self.resting_hr} bpm Ruhepuls")
        if self.hrv:             parts.append(f"📊 HRV: {self.hrv:.0f}")
        if self.weight:          parts.append(f"⚖️ {self.weight:.1f} kg")
        return " | ".join(parts)


@dataclass
class CalendarItem:
    title: str
    start: datetime
    end: Optional[datetime]
    all_day: bool
    calendar: Optional[str]
    location: Optional[str]
    uid: Optional[str] = None
    source: Optional[str] = None

    def format(self) -> str:
        time_str = _de_date(self.start, with_time=not self.all_day)
        if self.all_day:
            time_str += " (ganztägig)"
        loc = f" @ {self.location}" if self.location else ""
        cal = f" [{self.calendar}]" if self.calendar else ""
        return f"📅 {time_str} – {self.title}{loc}{cal}"


class DashboardReader:
    """Liest mantis-native Daten (Health + Kalender)."""

    def __init__(self):
        pass

    def close(self):
        pass

    # ── Health ────────────────────────────────────────────────────────────────
    def get_recent_health(self, days: int = 3) -> list[HealthSummary]:
        rows = health_d.recent(days=days)
        return [HealthSummary(
            date=r["date"], steps=r["steps"], active_calories=r["active_calories"],
            exercise_minutes=r["exercise_minutes"], sleep_duration=r["sleep_duration"],
            sleep_deep=r["sleep_deep"], resting_hr=r["resting_hr"],
            hrv=r["hrv"], weight=r["weight"],
        ) for r in rows]

    def refresh_health(self) -> int:
        """Health-Daten aus der COROS-Cloud nachziehen (früher: BodyOS-Poll)."""
        # Der Import steht bewusst in der Funktion: domains/coros/importer.py zieht
        # client.py und damit config nach, und tools/dashboard.py wird sehr früh importiert.
        from domains.coros import importer
        return importer.sync()

    # ── Kalender ────────────────────────────────────────────────────────────────
    def get_upcoming_events(self, days: int = 7) -> list[CalendarItem]:
        evs = calendar_d.upcoming(days=days)
        return [CalendarItem(
            title=e["title"], start=e["start"], end=e.get("end"),
            all_day=e["all_day"], calendar=e.get("calendar"), location=e.get("location"),
            uid=e.get("uid"), source=e.get("source"),
        ) for e in evs]

    def create_event(self, title: str, start, end=None, location: str = None,
                     notes: str = None, all_day: bool = False) -> str:
        return calendar_d.create_event(title=title, start=start, end=end,
                                       location=location, notes=notes, all_day=all_day)

    def update_event(self, query: str, title: str = None, start=None,
                     end=None, location: str = None) -> str:
        ev = calendar_d.find_event(query)
        if not ev:
            return f"Kein Termin gefunden für '{query}'."
        ok = calendar_d.update_event(ev.get("uid"), title=title, start=start, end=end,
                                     location=location, gcal_id=ev.get("gcal_id"))
        if not ok:
            return f"Termin '{ev['title']}' konnte nicht aktualisiert werden."
        return f"Termin '{ev['title']}' aktualisiert."

    def delete_event(self, query: str) -> str:
        ev = calendar_d.find_event(query)
        if not ev:
            return f"Kein Termin gefunden für '{query}'."
        title = ev.get("title", query)
        calendar_d.delete_event(uid=ev.get("uid"), gcal_id=ev.get("gcal_id"))
        return f"Termin '{title}' gelöscht."

    # ── Kontext für System-Prompt ──────────────────────────────────────────────
    def format_for_context(self) -> str:
        parts = []
        try:
            health = self.get_recent_health(days=2)
            if health:
                parts.append("### Gesundheit (letzte Tage)")
                parts += [h.format() for h in health]
        except Exception as e:
            log.debug(f"Health-Kontext: {e}")
        try:
            events = self.get_upcoming_events(days=3)
            if events:
                parts.append("\n### Kalender (nächste 3 Tage)")
                parts += [e.format() for e in events]
        except Exception as e:
            log.debug(f"Kalender-Kontext: {e}")
        return "\n".join(parts) if parts else "Keine Daten verfügbar."
