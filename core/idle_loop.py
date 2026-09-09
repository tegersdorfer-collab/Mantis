"""
IdleLoop — Autopilot-Ticks, Maintenance, Health-Refresh, Pattern-Detection, Monitoring.
Läuft als asyncio-Task wenn kein aktives Gespräch stattfindet.
"""
import asyncio
import logging
from datetime import datetime

from core import db, backup

log = logging.getLogger(__name__)


def _elapsed_since(last: "datetime | None", now: datetime, default: float = float("inf")) -> float:
    return (now - last).total_seconds() if last else default


def _safe_task(coro, label: str):
    async def _wrapper():
        try:
            await coro
        except Exception as e:
            log.warning(f"Background-Task '{label}' fehlgeschlagen: {e}", exc_info=True)
            db.log_error(label, e)
    return asyncio.create_task(_wrapper())


class IdleLoop:
    def __init__(self, autopilot, reflection, consolidator, forgetting,
                 kzg, lzg, dashboard, thermal, reminders,
                 proactive_engine, proactive_tracker,
                 bg_llm, chat_llm, suggest_one,
                 is_user_active,
                 pattern_detector_mod, generate_insight_task):
        self.autopilot           = autopilot
        self.reflection          = reflection
        self.consolidator        = consolidator
        self.forgetting          = forgetting
        self.kzg                 = kzg
        self.lzg                 = lzg
        self.dashboard           = dashboard
        self.thermal             = thermal
        self.reminders           = reminders
        self.proactive_engine    = proactive_engine
        self.proactive_tracker   = proactive_tracker
        self.bg_llm              = bg_llm
        self.chat_llm            = chat_llm
        self.suggest_one         = suggest_one
        self.is_user_active      = is_user_active
        self.pattern_detector    = pattern_detector_mod
        self.generate_insight    = generate_insight_task

        self._state = "idle"
        self._task: asyncio.Task | None = None

        self._last_consolidation: datetime | None = None
        self._last_health_refresh: datetime | None = None
        self._last_health_suggestion: datetime | None = None
        self._last_pattern_run: datetime | None = None
        self._last_insight_run: datetime | None = None
        self._last_plan_check: datetime | None = None
        self._last_news_refresh: datetime | None = None

    def resume(self) -> None:
        self._state = "idle"
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._loop())

    def pause(self) -> None:
        self._state = "conversation"
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while self._state == "idle":
            try:
                thermal_state = self.thermal.get_state()
                if thermal_state.paused:
                    await self.thermal.wait_until_cool()
                    continue
            except Exception as e:
                log.debug(f"Thermal-Check: {e}")

            await self._tick_autopilot()
            await self._tick_accountability()

            if not self.is_user_active():
                await self._tick_reflection()
                await self._tick_maintenance()
                await self._tick_health()
                await self._tick_patterns()
                await self._tick_insights()
                await self._tick_plan()
                await self._tick_news()

            await asyncio.sleep(self._next_delay())

    # ── Ticks ─────────────────────────────────────────────────────────────────

    async def _tick_autopilot(self) -> None:
        try:
            await self.autopilot.tick()
        except Exception as e:
            log.debug(f"Autopilot-Tick: {e}")
            db.log_error("Autopilot-Tick", e)

    async def _tick_accountability(self) -> None:
        """Accountability & Momentum (Daily Anchor + Der Block). Läuft IMMER —
        auch wenn Timo gerade aktiv ist —, denn die Prompts sind zeitgebunden.
        Sendet über den zentralen Autopilot-Pfad (Telegram + Web-Push)."""
        try:
            from domains import accountability
            await accountability.tick(self.autopilot._send, self.dashboard)
        except Exception as e:
            log.debug(f"Accountability-Tick: {e}")
            db.log_error("Accountability-Tick", e)

    async def _tick_reflection(self) -> None:
        try:
            await self.reflection.daily_reflection()
        except Exception as e:
            log.debug(f"Reflexion: {e}")
            db.log_error("Reflexion", e)

    async def _tick_news(self) -> None:
        """Stündlich den News-Globus aktualisieren (Feeds → geolokalisieren → Cache)."""
        now = datetime.now()
        if _elapsed_since(self._last_news_refresh, now) < 3600:
            return
        self._last_news_refresh = now
        try:
            from core import news_globe
            await news_globe.refresh()
        except Exception as e:
            log.debug(f"News-Globus-Refresh: {e}")
            db.log_error("News-Globus-Refresh", e)

    async def _tick_plan(self) -> None:
        """Alle 6h prüfen, ob ein neuer Trainingsplan fällig ist (≥42 Tage / keiner)."""
        now = datetime.now()
        if _elapsed_since(self._last_plan_check, now) < 21600:
            return
        self._last_plan_check = now
        try:
            from datetime import date as _date
            from domains import fitness, plan_generator
            if plan_generator.needs_regen(fitness.active_plan(), _date.today()):
                await plan_generator.generate_and_save(self.chat_llm, self.bg_llm)
        except Exception:
            log.exception("Plan-Auto-Generierung fehlgeschlagen")

    async def _tick_maintenance(self) -> None:
        now = datetime.now()
        if self._last_consolidation and (now - self._last_consolidation).total_seconds() < 3600:
            await self._maybe_kzg_checkpoint()
            return
        self._last_consolidation = now
        for coro, label in [
            (self.consolidator.consolidate_silent(), "Memory-Consolidator"),
            (self._run_forgetting(), "ForgettingCurve"),
            (asyncio.to_thread(backup.maybe_run_daily), "DB-Backup"),
            (asyncio.to_thread(backup.maybe_verify_weekly), "Backup-Restore-Check"),
        ]:
            try:
                await coro
            except Exception as e:
                log.debug(f"{label}: {e}")
                db.log_error(label, e)
        await self._maybe_kzg_checkpoint()

    async def _maybe_kzg_checkpoint(self) -> None:
        if not self.kzg.should_checkpoint():
            return
        turns = self.kzg.get_turns_for_summary()
        if not turns:
            return
        lines = "\n".join(
            f"{'Timo' if t.role == 'user' else 'Mantis'}: {t.content}" for t in turns
        )
        prompt = (
            "Fasse das folgende Gespräch prägnant auf Deutsch zusammen (max 200 Wörter). "
            "Behalte wichtige Fakten, Entscheidungen und Aufgaben.\n\n" + lines
        )
        try:
            summary = await self.bg_llm.complete(prompt, max_tokens=300)
            self.kzg.apply_checkpoint(summary, len(turns))
            log.info(f"📝 KZG-Checkpoint: {len(turns)} Turns komprimiert")
        except Exception as e:
            log.warning(f"KZG-Checkpoint fehlgeschlagen: {e}")

    async def _run_forgetting(self) -> None:
        stats = await self.forgetting.run()
        if stats["forgotten"] or stats["chat_pruned"]:
            log.info(f"🕐 Forgetting: {stats}")

    async def _tick_health(self) -> None:
        now = datetime.now()
        if _elapsed_since(self._last_health_refresh, now) < 1800:
            return
        self._last_health_refresh = now
        try:
            new_days = await asyncio.to_thread(self.dashboard.refresh_health)
            if not new_days or not self.proactive_tracker.can_send_data_event():
                return
            thought = await self.proactive_engine.generate()
            if thought and await self.proactive_engine.evaluate(thought):
                await self.autopilot._send(thought, kind="health_update")
                self.proactive_tracker.record_sent()
            if _elapsed_since(self._last_health_suggestion, now) > 86400:
                ok = await self.suggest_one(
                    f"Neue Gesundheitsdaten für {new_days} Tag(e) importiert",
                    self.bg_llm, self.lzg
                )
                if ok:
                    self._last_health_suggestion = now
        except Exception as e:
            log.debug(f"Health-Refresh: {e}")
            db.log_error("Health-Refresh", e)

    async def _tick_patterns(self) -> None:
        now = datetime.now()
        if _elapsed_since(self._last_pattern_run, now) < 86400:
            return
        self._last_pattern_run = now
        try:
            n = await self.pattern_detector.update_memories(self.lzg, self.bg_llm)
            if n:
                log.info(f"🔍 Pattern Detector: {n} neue Muster gespeichert")
        except Exception as e:
            log.debug(f"Pattern Detector: {e}")
            db.log_error("Pattern-Detector", e)

    async def _tick_insights(self) -> None:
        now = datetime.now()
        if _elapsed_since(self._last_insight_run, now) < 14400:
            return
        has_active = db.query(
            "SELECT 1 FROM tasks WHERE assigned_to='mantis' AND status NOT IN ('done','archived') "
            "AND (suggestion_status IS NULL OR suggestion_status='accepted') LIMIT 1"
        )
        if has_active:
            return
        self._last_insight_run = now
        try:
            await self.generate_insight(self.bg_llm, self.lzg)
        except Exception as e:
            log.debug(f"Insight-Engine: {e}")
            db.log_error("Insight-Engine", e)

    # ── Monitoring ────────────────────────────────────────────────────────────

    async def monitoring_loop(self) -> None:
        import httpx as _httpx
        _failures = 0
        await asyncio.sleep(60)
        while True:
            try:
                async with _httpx.AsyncClient(timeout=5) as c:
                    from web.client_auth import dashboard_url, dashboard_headers
                    r = await c.get(f"{dashboard_url()}/health", headers=dashboard_headers())
                checks = r.json() if r.status_code == 200 else {}
                unhealthy = [k for k, v in checks.items() if str(v).startswith("error")]
                if unhealthy:
                    _failures += 1
                    if _failures >= 2:
                        msg = f"⚠️ Mantis Health-Check: {', '.join(unhealthy)} fehlerhaft"
                        try:
                            from core.push import send_push
                            await asyncio.to_thread(send_push, "Mantis Health Alert", msg)
                        except Exception:
                            pass
                        log.warning(msg)
                else:
                    _failures = 0
            except Exception as e:
                log.debug(f"Monitoring: {e}")
            await asyncio.sleep(300)

    # ── Delay ─────────────────────────────────────────────────────────────────

    def _next_delay(self) -> int:
        try:
            nxt = self.reminders.next_due_seconds()
        except Exception:
            nxt = None
        has_active = db.query(
            "SELECT 1 FROM tasks WHERE assigned_to='mantis' AND status='in_progress' "
            "AND execution_phase IN ('executing','finalizing') LIMIT 1"
        )
        if has_active:
            return 8
        return 60 if nxt is None else max(5, min(60, nxt + 1))
