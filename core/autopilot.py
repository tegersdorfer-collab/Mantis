"""
Autopilot – die autonome Engine von Mantis.
Zeitbewusste, wertorientierte proaktive Aktivitäten statt simpler Zufallsgedanken:

- Morgen-Briefing (Wetter, Termine, Tasks, Habits, Ziel-Fokus)
- Abend-Review (Tagesrückblick, Mood-Check, Plan für morgen)
- Health-Anomalie-Erkennung (Schlaf/HRV-Einbrüche)
- Ziel-Checkins (bei Stillstand)
- Kalender-Vorbereitung (Termin morgen)
- Kontextueller proaktiver Gedanke (mit Selbstbewertung)

Completion-driven: läuft kontinuierlich, dedupliziert Tagesaktivitäten via Settings.
"""
import json
import logging
from datetime import date, datetime, timedelta

from core import db
from core.db import log_event
from core.status import BUS
from domains import habits, fitness, nutrition, goals, weather
import config

log = logging.getLogger(__name__)

# Gemeinsamer Stil-Constraint für alle proaktiven Nachrichten (lokales Modell).
# Wird als System-Message an jeden chat()-Aufruf gehängt — hebt die Qualität,
# ohne jeden einzelnen Prompt umzuschreiben.
STYLE = (
    "Du schreibst eine kurze Push-Nachricht an Timo. Strikte Regeln: "
    "Natürliches, fehlerfreies Deutsch in vollständigen Sätzen. "
    "Kein Englisch, keine erfundenen Abkürzungen, keine Wiederholungen, keine Floskeln, "
    "keine Entschuldigungen, kein Meta-Kommentar über dich selbst. "
    "Schreibe konkret, freundlich und direkt. Gib NUR die Nachricht aus — "
    "keine Anführungszeichen, keine Überschrift, kein einleitendes 'Hier ist'. "
    "Erfinde nichts dazu; wenn Daten fehlen, lass es weg. "
    "Formuliere Empfehlungen (Trainingsintensität, Tagesfokus, Pausentage) IMMER als Vorschlag "
    "('könnte heute...', 'würde sich anbieten...'), NIEMALS als bereits getroffene Entscheidung "
    "('heute ist Fokus auf...', 'wir machen einen ruhigen Tag'). Timo entscheidet, nicht du. "
    "Wenn eine Gewohnheit heute noch keinen Eintrag hat, heißt das 'noch nicht eingetragen', "
    "nicht 'nicht gemacht' — das weißt du nicht sicher, also unterstelle es nicht."
)

# Mindestlänge an echtem Text (ohne Emoji/Whitespace), damit eine Nachricht
# gesendet wird — fängt kaputte Modell-Fragmente wie "Liebe Zeit" ab.
_MIN_CONTENT_CHARS = 20


def _content_len(text: str) -> int:
    """Länge des eigentlichen Textinhalts ohne führende Emojis/Symbole/Whitespace."""
    stripped = text.strip()
    while stripped and not stripped[0].isalnum():
        stripped = stripped[1:].lstrip()
    return len(stripped)


class Autopilot:
    def __init__(self, llm, lzg, dashboard, reminders, channel, proactive, tracker,
                 identity: str, lock=None, is_user_active=None, search=None):
        self.llm = llm
        self.lzg = lzg
        self.dashboard = dashboard
        self.reminders = reminders
        self.channel = channel
        self.proactive = proactive
        self.tracker = tracker
        self.identity = identity
        self.lock = lock
        self.is_user_active = is_user_active or (lambda: False)
        self.search = search  # WebSearch-Instanz für Recherche

    # ── Dedup-Helfer (eine Aktivität pro Tag) ────────────────────────────────

    def _needs(self, key: str) -> bool:
        last = db.get_setting(f"autopilot_{key}_date")
        return last != str(date.today())

    def _mark(self, key: str) -> None:
        db.set_setting(f"autopilot_{key}_date", str(date.today()))

    # ── Senden + Persistenz ──────────────────────────────────────────────────

    async def _send(self, text: str, kind: str = "proactive") -> None:
        if not config.AUTONOMOUS_MESSAGES_ENABLED:
            log.info("Autonome Nachricht unterdrückt (deaktiviert): %s", kind)
            return
        if not text:
            return
        if _content_len(text) < _MIN_CONTENT_CHARS:
            log.warning(f"Proaktive Nachricht ({kind}) verworfen — zu kurz/kaputt: {text!r}")
            return
        await self.channel.send(text)
        try:
            from core import push
            import asyncio as _asyncio
            await _asyncio.to_thread(push.send_push, "Mantis", text, "/?view=chat")
        except Exception:
            pass
        try:
            # Damit proaktive Nachrichten auch im Desktop-HUD als Alert erscheinen
            # (SSE /api/status/stream), nicht nur via Telegram/Web-Push.
            BUS.emit("autopilot", text, detail=kind)
        except Exception:
            pass
        try:
            self.lzg.save_kzg_turn("assistant", text)
        except Exception:
            pass
        try:
            db.execute(
                "INSERT INTO chat_messages (role, content, channel, meta) VALUES (%s,%s,'autopilot',%s)",
                ("assistant", text, json.dumps({"kind": kind})),
            )
        except Exception:
            pass
        log_event(kind, text[:120])
        self.tracker.record_sent()

    # ── Haupt-Tick ────────────────────────────────────────────────────────────

    async def tick(self) -> bool:
        """Ein autonomer Schritt. Gibt True zurück wenn etwas gesendet wurde."""
        # 1. Fällige Reminder (zeitkritisch, immer – auch wenn User gerade aktiv)
        sent_reminder = False
        try:
            for r in self.reminders.get_due():
                await self._send(f"⏰ Erinnerung: {r.text}", kind="reminder")
                sent_reminder = True
        except Exception as e:
            log.debug(f"Reminder-Check: {e}")
        if sent_reminder:
            return True

        # Wenn Timo gerade aktiv ist: Modell für ihn freihalten, keine proaktive Last
        if self.is_user_active():
            return False

        hour = datetime.now().hour

        # 2. Morgen-Briefing (erst ab 6:35 – Health-Daten kommen um 6:30 rein)
        now_dt = datetime.now()
        if (now_dt.hour > 6 or (now_dt.hour == 6 and now_dt.minute >= 35)) and hour < 11 and self._needs("briefing"):
            try:
                async with self._guard():
                    await self._morning_briefing()
                self._mark("briefing")
                return True
            except Exception as e:
                log.warning(f"Briefing fehlgeschlagen: {e}")

        # 2b. Kalender-Optimierung morgens (nach Briefing, 1x/Tag)
        if (now_dt.hour > 6 or (now_dt.hour == 6 and now_dt.minute >= 35)) and hour < 11 and self._needs("cal_optimize"):
            try:
                async with self._guard():
                    await self._calendar_check()
                self._mark("cal_optimize")
                return True
            except Exception as e:
                log.warning(f"Kalender-Optimierung fehlgeschlagen: {e}")

        # 3. Abend-Review
        if 19 <= hour < 23 and self._needs("review"):
            try:
                async with self._guard():
                    await self._evening_review()
                self._mark("review")
                return True
            except Exception as e:
                log.warning(f"Review fehlgeschlagen: {e}")

        # 3b. Tägliche KI-Reflexion (22-23 Uhr, nach Abend-Review)
        if 22 <= hour < 23 and self._needs("ai_reflection"):
            try:
                async with self._guard():
                    await self._ai_daily_reflection()
                self._mark("ai_reflection")
            except Exception as e:
                log.warning(f"KI-Reflexion fehlgeschlagen: {e}")

        # 3g. Wetterbasiertes Coaching (morgens, 1x/Tag)
        if 7 <= hour < 10 and self._needs("weather_coaching"):
            try:
                async with self._guard():
                    msg = await self._weather_coaching()
                if msg:
                    await self._send(msg, kind="weather")
                self._mark("weather_coaching")
            except Exception as e:
                log.debug(f"Weather-Coaching: {e}")

        # 3f. Personal Newsletter (Freitag 17-18 Uhr, 1x/Woche)
        if now_dt.weekday() == 4 and 17 <= hour < 18 and self._needs("weekly_newsletter"):
            try:
                async with self._guard():
                    await self._personal_newsletter()
                self._mark("weekly_newsletter")
            except Exception as e:
                log.debug(f"Newsletter: {e}")

        # 3e. Periodische Themen-Recherche (Montag 8-9 Uhr, 1x/Woche)
        if now_dt.weekday() == 0 and 8 <= hour < 9 and self._needs("weekly_research"):
            try:
                async with self._guard():
                    await self._weekly_research()
                self._mark("weekly_research")
            except Exception as e:
                log.debug(f"Weekly-Research: {e}")

        # 3d. Proaktive Smart-Notifications (mittags, 1x/Tag)
        if 12 <= hour < 14 and self._needs("smart_notify"):
            try:
                async with self._guard():
                    await self._smart_notifications()
                self._mark("smart_notify")
            except Exception as e:
                log.debug(f"Smart-Notify: {e}")

        # 3c. Workout-Empfehlung basierend auf HRV + Schlaf (morgens 7-10 Uhr)
        if 7 <= hour < 10 and self._needs("workout_rec"):
            try:
                async with self._guard():
                    msg = await self._workout_recommendation()
                if msg:
                    await self._send(msg, kind="workout_rec")
                self._mark("workout_rec")
            except Exception as e:
                log.debug(f"Workout-Empfehlung: {e}")

        # 4. Health-Anomalie (tagsüber, einmal/Tag)
        if 8 <= hour < 21 and self._needs("health_alert"):
            try:
                async with self._guard():
                    msg = await self._health_anomaly()
                if msg:
                    await self._send(msg, kind="health")
                self._mark("health_alert")
                if msg:
                    return True
            except Exception as e:
                log.debug(f"Health-Anomalie: {e}")

        # 5. Mantis-Tasks abarbeiten (Deep Execution)
        try:
            active = db.query(
                """SELECT * FROM tasks WHERE assigned_to='mantis'
                   AND parent_id IS NULL
                   AND status NOT IN ('done','archived')
                   AND (suggestion_status IS NULL OR suggestion_status='accepted')
                   AND (hold_until IS NULL OR hold_until < NOW())
                   ORDER BY
                     CASE execution_phase
                       WHEN 'executing' THEN 0
                       WHEN 'pending'   THEN 1
                       ELSE 2
                     END,
                     priority DESC, created_at ASC
                   LIMIT 1"""
            )
            if active:
                task = active[0]
                async with self._guard():
                    await self._deep_execute_tick(task)
                return True
        except Exception as e:
            log.warning(f"Task-Executor: {e}")

        # 7. Kontextueller proaktiver Gedanke (mit Mindestabstand + Selbstbewertung)
        if not self.tracker.can_send():
            return False
        async with self._guard():
            if self.is_user_active():
                return False
            thought = await self.proactive.generate()
            if not thought:
                return False
            if await self.proactive.evaluate(thought):
                await self._send(thought, kind="thought")
                return True
            log_event("thought", f"verworfen: {thought[:80]}")
            return False

    async def _deep_execute_tick(self, task: dict) -> None:
        """Ein Tick der Deep-Execution: Planning → Clarification → Subtask → Finalize."""
        from domains.task_executor import plan_task, execute_next_subtask, finalize_task
        from core.status import BUS
        phase = task.get("execution_phase") or "pending"
        tid = task["id"]
        title_short = task["title"][:40] + ("…" if len(task["title"]) > 40 else "")

        # ── Phase 1: Planning ──────────────────────────────────────────────────
        if phase == "pending":
            log.info(f"📋 Plane Task: {task['title']}")
            BUS.emit("task_working", f"📋 Plant: {title_short}", detail=getattr(self.llm,'model_name',''))
            db.execute("UPDATE tasks SET execution_phase='planning', status='in_progress' WHERE id=%s", (tid,))
            # RAM freimachen falls bg_llm ein großes lokales Modell ist
            if hasattr(self.llm, 'unload_others'):
                await self.llm.unload_others()
            plan = await plan_task(task, self.llm, self.lzg)

            # Unteraufgaben anlegen
            for i, s in enumerate(plan.get("subtasks", [])):
                import json as _json
                notes_payload = _json.dumps({
                    "title": s["title"],
                    "needs_research": s.get("needs_research", False),
                    "research_query": s.get("research_query"),
                }, ensure_ascii=False)
                db.execute(
                    "INSERT INTO tasks (title, notes, parent_id, assigned_to, sort_order, status) VALUES (%s,%s,%s,'mantis',%s,'todo')",
                    (s["title"], notes_payload, tid, i)
                )

            # Rückfrage nötig?
            if plan.get("clarification_needed") and plan.get("clarification_question"):
                q = plan["clarification_question"]
                db.execute(
                    "UPDATE tasks SET execution_phase='waiting_clarification', clarification_question=%s, hold_until=NOW() + INTERVAL '24 hours' WHERE id=%s",
                    (q, tid)
                )
                await self._send(
                    f"❓ Zu deiner Aufgabe **{task['title']}**:\n\n{q}\n\n_(Antworte einfach hier – ich mache danach weiter)_",
                    kind="task_clarification"
                )
                log.info(f"🤔 Warte auf Klärung: {task['title']}")
            else:
                db.execute("UPDATE tasks SET execution_phase='executing' WHERE id=%s", (tid,))
                log.info(f"▶️  Starte Ausführung: {task['title']} ({len(plan.get('subtasks',[]))} Schritte)")

        # ── Phase 2: Warten auf Antwort ────────────────────────────────────────
        elif phase == "waiting_clarification":
            # Antwort ist in clarification_answer (gesetzt vom Message-Handler)
            if task.get("clarification_answer"):
                db.execute("UPDATE tasks SET execution_phase='executing', hold_until=NULL WHERE id=%s", (tid,))
                log.info(f"✅ Klärung erhalten, weiter: {task['title']}")
            # sonst einfach warten (hold_until läuft nach 24h ab → macht ohne Antwort weiter)
            elif not task.get("hold_until") or datetime.now().timestamp() > task.get("hold_until", datetime.now()).timestamp():
                db.execute("UPDATE tasks SET execution_phase='executing', hold_until=NULL WHERE id=%s", (tid,))

        # ── Phase 3: Schritt für Schritt ausführen ─────────────────────────────
        elif phase == "executing":
            # Nächsten Schritt für Status ermitteln
            next_sub = db.query(
                "SELECT title FROM tasks WHERE parent_id=%s AND status NOT IN ('done','archived') ORDER BY sort_order ASC, id ASC LIMIT 1",
                (tid,)
            )
            done_count = len(db.query("SELECT 1 FROM tasks WHERE parent_id=%s AND status='done'", (tid,)))
            total_count = len(db.query("SELECT 1 FROM tasks WHERE parent_id=%s", (tid,)))
            step_info = f"{done_count+1}/{total_count}" if total_count else ""
            sub_title = next_sub[0]["title"][:30] if next_sub else "…"
            BUS.emit("task_working", f"🤖 {step_info} {sub_title}", detail=getattr(self.llm,'model_name',''))
            if hasattr(self.llm, 'unload_others'):
                await self.llm.unload_others()
            search = self.search
            more = await execute_next_subtask(task, self.llm, search=search)
            if not more:
                # Alle Schritte fertig → synthese
                db.execute("UPDATE tasks SET execution_phase='finalizing' WHERE id=%s", (tid,))

        # ── Phase 4: Zusammenfassen & senden ──────────────────────────────────
        elif phase == "finalizing":
            BUS.emit("task_working", f"✍️ Fasst zusammen: {title_short}", detail=getattr(self.llm,'model_name',''))
            result = await finalize_task(task, self.llm)
            if result is None:
                # Alle Unteraufgaben sind fehlgeschlagen (z.B. LLM nicht erreichbar) –
                # NICHT als Erfolg melden, sondern ehrlich als Fehlschlag
                db.execute(
                    "UPDATE tasks SET status='archived', execution_phase='done', "
                    "rejection_reason='Alle Unteraufgaben fehlgeschlagen' WHERE id=%s",
                    (tid,)
                )
                await self._send(
                    f"⚠️ **{task['title']}** konnte nicht erledigt werden – alle Schritte sind fehlgeschlagen.",
                    kind="task_result"
                )
                log.warning(f"❌ Task fehlgeschlagen (alle Unteraufgaben archiviert): {task['title']}")
            else:
                db.execute(
                    "UPDATE tasks SET status='done', execution_phase='done', completed_at=NOW(), mantis_result=%s WHERE id=%s",
                    (result[:4000], tid)
                )
                # Kurze Zusammenfassung senden
                preview = result[:600] + ("…" if len(result) > 600 else "")
                await self._send(
                    f"✅ **{task['title']}** erledigt\n\n{preview}\n\n_(Vollständiges Ergebnis im Hub unter Aufgaben)_",
                    kind="task_result"
                )
                log.info(f"🎉 Task abgeschlossen: {task['title']}")

    def _guard(self):
        """Context-Manager: nutzt den geteilten LLM-Lock falls vorhanden."""
        import contextlib
        if self.lock is not None:
            return self.lock
        return contextlib.nullcontext()

    # ── Aktivitäten ────────────────────────────────────────────────────────────

    async def _gather(self) -> dict:
        """Sammelt Live-Kontext (parallel-tauglich, hier sequentiell weil DB sync)."""
        ctx = {}
        try:
            ctx["health"] = self.dashboard.get_recent_health(days=2)
        except Exception:
            ctx["health"] = []
        try:
            ctx["events"] = self.dashboard.get_upcoming_events(days=2)
        except Exception:
            ctx["events"] = []
        try:
            ctx["tasks"] = self.dashboard.get_open_tasks(limit=6)
        except Exception:
            ctx["tasks"] = []
        try:
            ctx["habits"] = habits.habit_overview()
        except Exception:
            ctx["habits"] = []
        try:
            ctx["goals"] = goals.list_goals()
        except Exception:
            ctx["goals"] = []
        try:
            ctx["weather"] = await weather.get_weather()
        except Exception:
            ctx["weather"] = None
        try:
            mems = self.lzg.get_all(limit=8)
            ctx["memories"] = self.lzg.format_for_context(mems)
        except Exception:
            ctx["memories"] = ""
        return ctx

    async def _morning_briefing(self) -> None:
        ctx = await self._gather()
        lines = []
        if ctx["weather"] and not ctx["weather"].get("error"):
            n = ctx["weather"]["now"]
            today = ctx["weather"]["forecast"][0] if ctx["weather"]["forecast"] else {}
            lines.append(f"Wetter: {n['temp']}°C, {n['desc']}; heute {today.get('min')}–{today.get('max')}°C, Regen {today.get('rain_prob')}%")
        if ctx["events"]:
            today_date = date.today()
            tomorrow_date = today_date + timedelta(days=1)
            event_parts = []
            for e in ctx["events"][:5]:
                e_date = e.start.date() if hasattr(e.start, "date") else e.start
                if e_date == today_date:
                    day_label = "heute"
                elif e_date == tomorrow_date:
                    day_label = "morgen"
                else:
                    day_label = e_date.strftime("%d.%m.")
                time_str = "" if e.all_day else f" {e.start.strftime('%H:%M')} Uhr"
                event_parts.append(f"{day_label}{time_str}: {e.title}")
            lines.append("Termine: " + "; ".join(event_parts))
        if ctx["tasks"]:
            lines.append("Top-Aufgaben: " + "; ".join(t.title for t in ctx["tasks"][:4]))
        if ctx["habits"]:
            offen = [h["name"] for h in ctx["habits"] if not h["today_done"]]
            if offen:
                lines.append("Offene Gewohnheiten heute: " + ", ".join(offen))
        if ctx["health"]:
            h = ctx["health"][0]
            if h.sleep_duration:
                lines.append(f"Letzte Nacht: {h.sleep_duration:.1f}h Schlaf")
        if ctx["goals"]:
            lines.append("Ziele: " + "; ".join(f"{g['title']} ({g['progress_pct']}%)" for g in ctx["goals"][:3]))

        facts = "\n".join(lines) if lines else "Keine besonderen Daten."
        prompt = (
            f"{self.identity}\n\n"
            "Erstelle ein kurzes, energetisches Morgen-Briefing für Timo auf Deutsch. "
            "Max 5-6 Zeilen. Konkret, motivierend, kein Smalltalk. "
            "Beginne mit 'Guten Morgen'. Hebe das Wichtigste hervor und schlage EINEN möglichen "
            "Fokus für den Tag vor — als Idee, nicht als Ansage, Timo entscheidet selbst. "
            "Schreibe jeden Punkt nur EINMAL. Keine Wiederholungen, keine Selbstkorrekturen.\n\n"
            f"Daten von heute:\n{facts}\n\nBriefing:"
        )
        text = await self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                   system=STYLE, temperature=0.6, max_tokens=300)
        await self._send("☀️ " + text.strip(), kind="briefing")
        log.info("☀️ Morgen-Briefing gesendet")

    async def _evening_review(self) -> None:
        ctx = await self._gather()
        # Tagesleistung sammeln
        done_habits = [h["name"] for h in ctx["habits"] if h["today_done"]]
        open_habits = [h["name"] for h in ctx["habits"] if not h["today_done"]]
        try:
            workouts_today = [w for w in fitness.recent_workouts(5) if str(w["date"]) == str(date.today())]
        except Exception:
            workouts_today = []
        nut = nutrition.day_totals()

        lines = []
        if done_habits:
            lines.append(f"Erledigte Gewohnheiten: {', '.join(done_habits)}")
        if open_habits:
            lines.append(f"Nicht erledigt: {', '.join(open_habits)}")
        if workouts_today:
            lines.append(f"Training: {', '.join(w['title'] for w in workouts_today)}")
        if nut.get("kcal"):
            lines.append(f"Ernährung: {int(nut['kcal'])} kcal, {int(nut['protein'])}g Protein")
        if ctx["events"]:
            tomorrow = [e for e in ctx["events"]
                        if e.start.date() == date.today() + timedelta(days=1)]
            if tomorrow:
                lines.append("Morgen: " + "; ".join(e.title for e in tomorrow[:3]))

        facts = "\n".join(lines) if lines else "Wenig Daten erfasst."
        prompt = (
            f"{self.identity}\n\n"
            "Erstelle einen kurzen Abend-Review für Timo auf Deutsch. Max 4-5 Zeilen. "
            "Würdige kurz was lief, sprich offen an was liegen blieb (direkt, respektvoll). "
            "Stelle GENAU EINE konkrete Frage zum Tag. "
            "Schreibe jeden Punkt nur EINMAL. Keine Wiederholungen, keine Entschuldigungen.\n\n"
            f"Tagesdaten:\n{facts}\n\nReview:"
        )
        text = await self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                   system=STYLE, temperature=0.6, max_tokens=300)
        await self._send("🌙 " + text.strip(), kind="review")
        log.info("🌙 Abend-Review gesendet")

    async def _health_anomaly(self) -> str | None:
        """Erkennt auffällige Health-Werte und formuliert ggf. einen Hinweis."""
        health = self.dashboard.get_recent_health(days=5)
        if len(health) < 2:
            return None
        latest = health[0]
        flags = []
        # Schlaf
        sleeps = [h.sleep_duration for h in health if h.sleep_duration]
        if latest.sleep_duration and len(sleeps) >= 2:
            avg = sum(sleeps[1:]) / max(1, len(sleeps[1:]))
            if latest.sleep_duration < 6 and latest.sleep_duration < avg - 1:
                flags.append(f"nur {latest.sleep_duration:.1f}h Schlaf (Schnitt {avg:.1f}h)")
        # HRV
        hrvs = [h.hrv for h in health if h.hrv]
        if latest.hrv and len(hrvs) >= 3:
            avg_hrv = sum(hrvs[1:]) / max(1, len(hrvs[1:]))
            if latest.hrv < avg_hrv * 0.8:
                flags.append(f"HRV deutlich gesunken ({latest.hrv:.0f} vs. Schnitt {avg_hrv:.0f})")
        # Ruhepuls
        hrs = [h.resting_hr for h in health if h.resting_hr]
        if latest.resting_hr and len(hrs) >= 3:
            avg_hr = sum(hrs[1:]) / max(1, len(hrs[1:]))
            if latest.resting_hr > avg_hr + 7:
                flags.append(f"erhöhter Ruhepuls ({latest.resting_hr} vs. {avg_hr:.0f})")
        if not flags:
            return None
        prompt = (
            f"{self.identity}\n\n"
            "Formuliere einen kurzen Hinweis (2-3 Sätze) zu Timos Gesundheit. "
            "Sachlich, kein Alarmismus, ein konkreter Vorschlag. "
            "Schreibe jeden Satz nur EINMAL.\n\n"
            f"Auffälligkeiten: {'; '.join(flags)}\n\nHinweis:"
        )
        text = await self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                   system=STYLE, temperature=0.5, max_tokens=150)
        return "🩺 " + text.strip()

    async def _calendar_check(self) -> None:
        """Analysiert den heutigen Tagesplan und meldet Konflikte / Vorschläge."""
        from domains.calendar_optimizer import analyze_day
        ctx = await self._gather()
        events = ctx.get("events", [])
        if not events:
            return
        result = await analyze_day(events, self.llm)
        summary = result.get("summary", "")
        if not summary or summary == "Plan sieht gut aus." or "Keine Termine" in summary:
            log.debug("Kalender-Check: kein Handlungsbedarf")
            return
        await self._send("📅 " + summary, kind="calendar_check")
        log.info("📅 Kalender-Check gesendet")

    async def _weather_coaching(self) -> str | None:
        """Wetter-basiertes Coaching: passt Outdoor-Empfehlungen ans Wetter an."""
        try:
            from domains import weather as _weather
            w = await _weather.get_weather()
        except Exception:
            return None
        if not w:
            return None

        if w.get("error"):
            return None
        now = w.get("now") or {}
        temp  = now.get("temp")
        desc  = (now.get("desc") or "").lower()
        rain  = any(k in desc for k in ["rain", "regen", "shower", "drizzle", "storm"])
        hot   = temp and float(temp) > 30
        cold  = temp and float(temp) < 5

        if rain:
            tip = "☔ Heute Regen → perfekter Tag für Indoor-Training oder Skill-Building statt Outdoor."
        elif hot:
            tip = f"🌡️ {temp}°C heute → früh morgens oder abends trainieren, viel Wasser trinken."
        elif cold:
            tip = f"🧊 {temp}°C → gut aufwärmen vor dem Training, Layering beim Sport draußen."
        else:
            tip = None

        if not tip:
            return None

        prompt = (
            f"{self.identity}\n\nWetter heute: {temp}°C, {desc}.\n"
            f"Gib Timo einen kurzen, konkreten Tagesplan-Hinweis (1-2 Sätze) basierend auf dem Wetter. "
            f"Bezug: {tip}\nDirekt, praktisch."
        )
        text = await self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                   system=STYLE, temperature=0.5, max_tokens=100)
        return "🌤️ " + text.strip()

    async def _personal_newsletter(self) -> None:
        """
        Freitags: Wochenzusammenfassung als Digest via Telegram.
        Enthält: erledigte Tasks, Health-Highlights, Habit-Streak, Brain-Notizen der Woche.
        """
        import asyncio
        lines = ["📰 **Wochenzusammenfassung**\n"]

        # Tasks diese Woche erledigt
        try:
            done = await asyncio.to_thread(db.query,
                """SELECT title FROM tasks WHERE status='done'
                   AND completed_at >= NOW() - INTERVAL '7 days'
                   ORDER BY completed_at DESC LIMIT 8"""
            )
            if done:
                lines.append("✅ **Erledigte Tasks:**")
                lines.extend(f"  · {t['title']}" for t in done)
        except Exception as e:
            log.warning(f"Newsletter/Tasks: {e}")

        # Health-Highlights
        try:
            health = self.dashboard.get_recent_health(days=7)
            if health:
                avg_sleep = sum(h.sleep_duration for h in health if h.sleep_duration) / max(1, sum(1 for h in health if h.sleep_duration))
                avg_hrv   = sum(h.hrv for h in health if h.hrv) / max(1, sum(1 for h in health if h.hrv))
                avg_steps = sum(h.steps for h in health if h.steps) / max(1, sum(1 for h in health if h.steps))
                lines.append(f"\n💪 **Health-Schnitt:** Schlaf {avg_sleep:.1f}h | HRV {avg_hrv:.0f} | Schritte {avg_steps:.0f}")
        except Exception as e:
            log.warning(f"Newsletter/Health: {e}")

        # Habit-Streaks
        try:
            habits = await asyncio.to_thread(db.query,
                """SELECT h.name, COUNT(l.id) as count FROM habits h
                   LEFT JOIN habit_logs l ON l.habit_id=h.id
                        AND l.date >= CURRENT_DATE-7 AND l.done=TRUE
                   WHERE h.active=TRUE GROUP BY h.name ORDER BY count DESC LIMIT 5"""
            )
            if habits:
                lines.append("\n🔁 **Habits (letzte 7 Tage):**")
                lines.extend(f"  · {h['name']}: {h['count']}/7" for h in habits)
        except Exception as e:
            log.warning(f"Newsletter/Habits: {e}")

        # Neue Brain-Notizen
        try:
            from domains.second_brain import get_all
            new_notes = [n for n in get_all(limit=50)
                         if hasattr(n, 'created_at') and
                         (datetime.now() - n.created_at.replace(tzinfo=None)).days <= 7][:5]
            if new_notes:
                lines.append("\n🧠 **Neue Notizen:**")
                lines.extend(f"  · {n.title}" for n in new_notes)
        except Exception as e:
            log.warning(f"Newsletter/Brain: {e}")

        msg = "\n".join(lines)
        await self._send(msg, kind="newsletter")
        log.info("📰 Personal Newsletter gesendet")

    async def _weekly_research(self) -> None:
        """
        Montags: durchsucht gespeicherte Recherche-Queries (brain_notes Kategorie 'resource'
        mit Tag 'research_query'), führt Web-Suche durch, speichert Zusammenfassung.
        """
        from domains.second_brain import get_by_category, add_note

        queries = [
            n for n in get_by_category("resource", limit=50)
            if "research_query" in (n.tags or [])
        ]
        if not queries:
            log.debug("Weekly-Research: keine Queries gespeichert")
            return

        results = []
        for note in queries[:3]:
            query = note.title
            prompt = (
                f"{self.identity}\n\n"
                f"Führe eine kurze Recherche zu folgendem Thema durch: '{query}'\n"
                "Fasse in 3-5 Sätzen zusammen was aktuell relevant/neu ist. "
                "Nutze dein Wissen bis August 2025. Antworte auf Deutsch."
            )
            try:
                summary = await self.llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    system=STYLE, temperature=0.4, max_tokens=200,
                )
                results.append(f"**{query}**\n{summary.strip()}")
            except Exception:
                pass

        if results:
            from datetime import date
            title = f"Wöchentliche Recherche {date.today().isoformat()}"
            content = "# Wöchentliche Themen-Recherche\n\n" + "\n\n---\n\n".join(results)
            add_note(title=title, content=content, category="resource",
                     tags=["weekly", "research", "auto"])
            await self._send(
                f"🔍 Wöchentliche Recherche: {len(results)} Themen zusammengefasst → Brain",
                kind="research"
            )

    async def _smart_notifications(self) -> None:
        """
        Proaktive Smart-Notifications: prüft Bedingungen die Timo vielleicht vergessen hat.
        - Tasks die diese Woche fällig sind aber noch nicht gestartet
        - Habits die seit 3+ Tagen nicht gemacht wurden
        - Offene Arzt-/Termin-Todos aus Memories
        """
        import asyncio
        msgs = []

        # Tasks die heute/morgen fällig sind und noch offen
        try:
            overdue = await asyncio.to_thread(db.query,
                """SELECT title, due FROM tasks
                   WHERE status NOT IN ('done','archived')
                   AND due <= CURRENT_DATE + 2
                   AND (assigned_to IS NULL OR assigned_to != 'mantis')
                   ORDER BY due ASC LIMIT 5"""
            )
            if overdue:
                items = "; ".join(f"{t['title']} ({t['due']})" for t in overdue[:3])
                msgs.append(f"⚡ Bald fällig: {items}")
        except Exception as e:
            log.warning(f"Smart-Notify/Tasks: {e}")

        # Habits 3+ Tage nicht gemacht
        try:
            stale_habits = await asyncio.to_thread(db.query,
                """SELECT name FROM habits h
                   WHERE active = TRUE
                   AND NOT EXISTS (
                       SELECT 1 FROM habit_logs l
                       WHERE l.habit_id = h.id AND l.date >= CURRENT_DATE - 3
                   )
                   LIMIT 3"""
            )
            if stale_habits:
                names = ", ".join(h["name"] for h in stale_habits)
                msgs.append(f"🔁 Seit 3+ Tagen kein Eintrag bei: {names} — nachgetragen oder pausiert?")
        except Exception as e:
            log.warning(f"Smart-Notify/Habits: {e}")

        for msg in msgs:
            await self._send(msg, kind="smart_notify")

    async def _workout_recommendation(self) -> str | None:
        """Morgens: HRV + Schlaf → Trainingsempfehlung (intensiv / moderat / Pause)."""
        health = self.dashboard.get_recent_health(days=7)
        if len(health) < 2:
            return None
        latest = health[0]
        if not latest.hrv and not latest.sleep_duration:
            return None

        hrvs   = [h.hrv for h in health if h.hrv]
        sleeps = [h.sleep_duration for h in health if h.sleep_duration]
        avg_hrv   = sum(hrvs[1:]) / max(1, len(hrvs[1:])) if len(hrvs) > 1 else None
        avg_sleep = sum(sleeps[1:]) / max(1, len(sleeps[1:])) if len(sleeps) > 1 else None

        hrv_ratio   = latest.hrv / avg_hrv if avg_hrv and latest.hrv else 1.0
        sleep_ok    = latest.sleep_duration >= 7.0 if latest.sleep_duration else True

        if hrv_ratio >= 1.05 and sleep_ok:
            intensity = "intensiv"
        elif hrv_ratio >= 0.9 and sleep_ok:
            intensity = "moderat"
        else:
            intensity = "Pause/Regeneration"

        facts = []
        if latest.hrv:
            facts.append(f"HRV heute: {latest.hrv:.0f}" + (f" (Schnitt {avg_hrv:.0f})" if avg_hrv else ""))
        if latest.sleep_duration:
            facts.append(f"Schlaf: {latest.sleep_duration:.1f}h" + (f" (Schnitt {avg_sleep:.1f}h)" if avg_sleep else ""))
        facts.append(f"Empfohlene Intensität: {intensity}")

        prompt = (
            f"{self.identity}\n\n"
            "Formuliere eine kurze Trainingsempfehlung für Timo (2-3 Sätze). "
            "Begründe sie mit den Daten. Schlage konkret vor was er heute machen könnte. "
            "Direkt, motivierend, kein Bla-Bla.\n\n"
            f"Daten:\n" + "\n".join(facts) + "\n\nEmpfehlung:"
        )
        text = await self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                   system=STYLE, temperature=0.5, max_tokens=150)
        return "🏋️ " + text.strip()

    async def _ai_daily_reflection(self) -> None:
        """
        Tägliche KI-Reflexion (22 Uhr): Analysiert Wins, Risiken und Muster
        über Habits, Health, Tasks und Memories. Speichert Ergebnis in brain_notes.
        """
        import asyncio
        ctx = await self._gather()

        # Daten zusammenstellen
        lines: list[str] = []

        # Habits
        done_h = [h["name"] for h in ctx.get("habits", []) if h.get("today_done")]
        open_h = [h["name"] for h in ctx.get("habits", []) if not h.get("today_done")]
        if done_h:
            lines.append(f"Erledigte Habits: {', '.join(done_h)}")
        if open_h:
            lines.append(f"Offene Habits: {', '.join(open_h)}")

        # Health
        try:
            health = self.dashboard.get_recent_health(days=7)
            if health:
                h = health[0]
                if h.hrv:
                    lines.append(f"HRV heute: {h.hrv:.0f}")
                if h.sleep_duration:
                    lines.append(f"Schlaf heute: {h.sleep_duration:.1f}h")
                if h.steps:
                    lines.append(f"Schritte: {h.steps}")
        except Exception:
            pass

        # Tasks (erledigt heute)
        try:
            done_tasks = await asyncio.to_thread(
                db.query,
                "SELECT title FROM tasks WHERE status='done' AND completed_at::date=CURRENT_DATE LIMIT 10",
            )
            if done_tasks:
                lines.append(f"Heute erledigte Tasks: {', '.join(t['title'] for t in done_tasks)}")
        except Exception:
            pass

        # Letzte Memories (Muster-Input)
        try:
            recent_mems = await asyncio.to_thread(self.lzg.get_all, limit=15)
            if recent_mems:
                lines.append("Letzte Erkenntnisse: " + "; ".join(m.content[:60] for m in recent_mems[:5]))
        except Exception:
            pass

        if not lines:
            log.debug("KI-Reflexion: zu wenig Daten")
            return

        facts = "\n".join(lines)
        prompt = (
            f"{self.identity}\n\n"
            "Du bist Mantis' End-of-Day-Reflexions-Engine. Analysiere Timos heutigen Tag. "
            "Identifiziere: 1) Klare Wins, 2) Wiederkehrende Muster (positiv/negativ), "
            "3) Ein konkretes Risiko für morgen, 4) Eine messbare Verbesserungsidee. "
            "Schreibe präzise, direkt, max. 5-6 Sätze. Keine Floskeln.\n\n"
            f"Tagesdaten:\n{facts}\n\nReflexion:"
        )
        reflection = await self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=STYLE, temperature=0.4, max_tokens=250,
        )
        reflection = reflection.strip()

        # In brain_notes als Daily-Eintrag speichern (ensure_today_daily → BrainNote-Dataclass)
        try:
            from domains import second_brain as _brain
            today_note = await asyncio.to_thread(_brain.ensure_today_daily)
            await asyncio.to_thread(
                _brain.update_note,
                today_note.id,
                content=today_note.content + f"\n\n### 🤖 KI-Reflexion\n{reflection}",
            )
        except Exception as e:
            log.warning(f"KI-Reflexion konnte nicht ins Brain gespeichert werden: {e}")

        log.info("🔍 Tägliche KI-Reflexion gespeichert")
        # Kein Telegram-Push — Reflexion ist intern, sichtbar im Brain-Dashboard
