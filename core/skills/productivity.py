"""
Productivity-Tools — aus core/skills.py extrahiert (verhaltensgleich).
Registriert sich via @T.register beim Import (durch core/skills/__init__.py).
"""
import logging

from core import tools as T
from core.timeparse import parse_datetime
from core.skill_context import CTX
from domains import tasks as tasks_d

log = logging.getLogger("core.skills")


@T.register("create_task",
    "Erstellt eine Aufgabe. kind='project' für größere Vorhaben (mit Unteraufgaben), "
    "kind='checklist' für Listen, sonst 'task'.",
    {"title": {"type": "string"},
     "priority": {"type": "string", "description": "high | medium | low"},
     "kind": {"type": "string", "description": "task | project | checklist"},
     "due": {"type": "string", "description": "Fälligkeit, z.B. 'morgen 9:00' oder '15.06.2026'"}},
    ["title"], "productivity")
async def _create_task(title: str, priority: str = "medium", kind: str = "task", due: str = None, notes: str = None):
    due_dt = parse_datetime(due) if due else None
    tid = tasks_d.create_task(title=title, notes=notes, priority=priority, kind=kind, due=due_dt)
    # Auto-Klassifikation: kann Mantis diese Task erledigen?
    assignee = "user"
    if CTX.llm:
        try:
            from domains.task_executor import classify
            assignee = await classify(title, notes, CTX.llm)
            from core import db as _db
            _db.execute("UPDATE tasks SET assigned_to=%s WHERE id=%s", (assignee, tid))
        except Exception:
            pass
    who = "Mantis übernimmt das" if assignee == "mantis" else "für dich eingetragen"
    return f"Aufgabe erstellt: {title} ({priority}) – {who}"

@T.register("add_subtask",
    "Fügt einer bestehenden Aufgabe/einem Projekt eine Unteraufgabe hinzu (sucht Eltern per Titel).",
    {"parent_query": {"type": "string", "description": "Teil des Titels der Hauptaufgabe"},
     "title": {"type": "string"}},
    ["parent_query", "title"], "productivity")
async def _add_subtask(parent_query: str, title: str):
    parent = tasks_d.find_task(parent_query)
    if not parent:
        return f"Hauptaufgabe '{parent_query}' nicht gefunden."
    tasks_d.create_task(title=title, parent_id=parent["id"])
    return f"Unteraufgabe '{title}' zu '{parent['title']}' hinzugefügt."

@T.register("complete_task", "Markiert eine Aufgabe als erledigt (sucht per Titel).",
    {"query": {"type": "string"}}, ["query"], "productivity")
async def _complete_task(query: str):
    task = tasks_d.find_task(query)
    if not task:
        return f"Keine offene Aufgabe gefunden für '{query}'."
    tasks_d.complete_task(task["id"])
    return f"Erledigt: {task['title']}"

@T.register("set_task_progress", "Setzt den Fortschritt einer Aufgabe in Prozent (sucht per Titel).",
    {"query": {"type": "string"}, "progress_pct": {"type": "integer", "description": "0-100"}},
    ["query", "progress_pct"], "productivity")
async def _set_task_progress(query: str, progress_pct: int):
    task = tasks_d.find_task(query)
    if not task:
        return f"Aufgabe '{query}' nicht gefunden."
    tasks_d.set_progress(task["id"], progress_pct)
    return f"'{task['title']}' auf {progress_pct}% gesetzt."

@T.register("list_tasks", "Listet Timos offene Aufgaben (mit Unteraufgaben & Fortschritt).",
    {}, [], "productivity")
async def _list_tasks():
    return tasks_d.context_summary(15) or "Keine offenen Aufgaben."

@T.register("set_reminder",
    "Setzt eine zeitgesteuerte Erinnerung.",
    {"text": {"type": "string"},
     "at": {"type": "string", "description": "Zeitpunkt, z.B. '15:30', 'morgen 8:00', 'in 2 stunden'"}},
    ["text", "at"], "productivity")
async def _set_reminder(text: str, at: str, kind: str | None = None):
    if not CTX.reminders:
        return "Reminder nicht verfügbar."
    dt = parse_datetime(at)
    if not dt:
        return f"Konnte Zeit '{at}' nicht verstehen."
    CTX.reminders.add(text, dt, kind="timer" if kind == "timer" else None)
    return f"Erinnerung gesetzt: '{text}' am {dt.strftime('%d.%m. %H:%M')}"


@T.register("stop_timer", "Stoppt den nächsten offenen Voice-Timer.", {}, [], "productivity")
async def _stop_timer():
    if not CTX.reminders:
        return "Timer nicht verfügbar."
    if CTX.reminders.stop_next_timer():
        return "Timer gestoppt."
    return "Kein aktiver Timer."

@T.register("create_calendar_event",
    "Erstellt einen Kalendereintrag.",
    {"title": {"type": "string"},
     "start": {"type": "string", "description": "Startzeit, z.B. 'morgen 14:00'"},
     "end": {"type": "string", "description": "Endzeit (optional)"},
     "location": {"type": "string"}},
    ["title", "start"], "productivity")
async def _create_event(title: str, start: str, end: str = None, location: str = None):
    if not CTX.dashboard:
        return "Kalender nicht verfügbar."
    s = parse_datetime(start)
    if not s:
        return f"Konnte Startzeit '{start}' nicht verstehen."
    e = parse_datetime(end) if end else None
    CTX.dashboard.create_event(title=title, start=s, end=e, location=location)
    return f"Termin erstellt: {title} am {s.strftime('%d.%m. %H:%M')}"

@T.register("get_calendar", "Zeigt anstehende Termine.",
    {"days": {"type": "integer", "description": "Anzahl Tage voraus"}}, [], "productivity")
async def _get_calendar(days: int = 7):
    if not CTX.dashboard:
        return "Kalender nicht verfügbar."
    events = CTX.dashboard.get_upcoming_events(days=days)
    return "\n".join(e.format() for e in events) if events else "Keine Termine."

@T.register("update_calendar_event",
    "Ändert einen bestehenden Kalendereintrag (Titel, Zeit, Ort). Nutze dies wenn Timo "
    "sagt 'verschieb', 'ändere', 'verlege' einen Termin. Sucht per Titel-Stichwort.",
    {"query": {"type": "string", "description": "Stichwort aus dem Termintitel"},
     "title": {"type": "string", "description": "Neuer Titel (optional)"},
     "start": {"type": "string", "description": "Neue Startzeit (optional)"},
     "end": {"type": "string", "description": "Neue Endzeit (optional)"},
     "location": {"type": "string", "description": "Neuer Ort (optional)"}},
    ["query"], "productivity")
async def _update_event(query: str, title: str = None, start: str = None,
                        end: str = None, location: str = None):
    if not CTX.dashboard:
        return "Kalender nicht verfügbar."
    s = parse_datetime(start) if start else None
    e = parse_datetime(end) if end else None
    return CTX.dashboard.update_event(query, title=title, start=s, end=e, location=location)

@T.register("delete_calendar_event",
    "Löscht einen Kalendereintrag. Nutze dies wenn Timo sagt 'lösch den Termin', "
    "'streich den Termin', 'cancel', 'absagen' o.ä. Sucht per Titel-Stichwort.",
    {"query": {"type": "string", "description": "Stichwort aus dem Termintitel, z.B. 'Zahnarzt' oder 'Mantis Test'"}},
    ["query"], "productivity")
async def _delete_event(query: str):
    if not CTX.dashboard:
        return "Kalender nicht verfügbar."
    return CTX.dashboard.delete_event(query)

@T.register("reschedule_after_overrun",
    "Verschiebt flexible Folge-Termine wenn ein Termin länger gedauert hat als geplant. "
    "Nutze dies wenn Timo sagt 'das Meeting hat X Minuten länger gedauert', "
    "'wir sind überzogen', 'hat länger als geplant gedauert' o.ä.",
    {
        "event_title": {"type": "string", "description": "Titel des Termins der überzogen hat"},
        "overrun_minutes": {"type": "integer", "description": "Wie viele Minuten länger als geplant"},
    },
    ["event_title", "overrun_minutes"], "productivity")
async def _reschedule_overrun(event_title: str, overrun_minutes: int):
    if not CTX.dashboard:
        return "Kalender nicht verfügbar."
    from domains.calendar_optimizer import reschedule_after_overrun
    events = CTX.dashboard.get_upcoming_events(days=1)
    return await reschedule_after_overrun(event_title, overrun_minutes, events, CTX.dashboard, CTX.llm)
