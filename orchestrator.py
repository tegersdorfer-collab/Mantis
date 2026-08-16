"""
Orchestrator — schlanke Fassade die alle Teilmodule zusammensteckt.

Aufgaben:
  - Instanziierung und Verdrahtung aller Komponenten
  - Öffentliche API für API-Server und Channel
  - start() / stop() Lifecycle

Eigene Logik ausgelagert in:
  core/prompt_builder.py  — System-Prompt-Aufbau
  core/message_handler.py — Nachrichtenverarbeitung + Streaming
  core/idle_loop.py       — Autopilot-Ticks + Maintenance + Monitoring
"""
import asyncio
import logging
import time
from pathlib import Path

from llm.base import LLMProvider
from llm.local import OllamaProvider
from core.backends.base import AgentBackend
from memory.kzg import KZG
from memory.lzg import LZG
from memory.consolidator import Consolidator
from memory.extractor import MemoryExtractor
from memory.knowledge import KnowledgeGraph
from memory.forgetting import ForgettingCurve
from communication.base import CommunicationChannel, IncomingMessage
from thermal import ThermalMonitor
from proactive import ProactiveEngine, ProactiveTracker

from core import db, fast, skills
from core.agent import Agent
from core.skills import SkillContext
from core.autopilot import Autopilot
from core.reflection import Reflection
from core.db import log_event
from core.skill_factory import load_all_on_startup
from core.prompt_builder import PromptBuilder
from core.message_handler import MessageHandler
from core.idle_loop import IdleLoop

from tools.search import WebSearch
from tools.dashboard import DashboardReader
from tools.reminders import ReminderStore
from domains import pattern_detector, alphaprogression
from domains.task_executor import suggest_one
from domains.insight_engine import generate_insight_task
from core.container import services
import config

log = logging.getLogger(__name__)


def _load_identity() -> str:
    text = (Path(__file__).parent / "identity" / "mantis.md").read_text()
    text = text.replace("{{OWNER}}", config.OWNER_NAME)
    text = text.replace("{{TIMEZONE}}", config.OWNER_TIMEZONE)
    if config.OWNER_EMAIL:
        text += f"\n- Kontakt: {config.OWNER_EMAIL}\n"
    return text


IDENTITY = _load_identity()


class Orchestrator:
    def __init__(self, channel: CommunicationChannel, lzg: LZG, thermal: ThermalMonitor,
                 chat_llm: LLMProvider, bg_llm: LLMProvider, embed_llm: LLMProvider,
                 agent_backend: AgentBackend, voice_agent_backend: AgentBackend | None = None):
        self.chat_llm  = chat_llm
        self.bg_llm    = bg_llm
        self.embed_llm = embed_llm
        self.channel   = channel
        self.lzg       = lzg
        self.thermal   = thermal

        # ── Kern-Komponenten ─────────────────────────────────────────────────
        self.kzg          = KZG()
        self.kg           = KnowledgeGraph()
        self.consolidator = Consolidator(llm=bg_llm, lzg=lzg)
        self.forgetting   = ForgettingCurve()
        self.extractor    = MemoryExtractor(llm_provider=embed_llm, lzg=lzg, kg=self.kg)
        self.reflection   = Reflection(llm=bg_llm, lzg=lzg)
        self.agent        = Agent(backend=agent_backend, max_steps=8)
        # Eigener, leichtgewichtiger Agent für Voice-Antworten (kleines, dauerhaft
        # geladenes Modell statt des großen Dashboard-Agenten) — fällt auf den
        # Haupt-Agenten zurück, falls kein dediziertes Voice-Backend übergeben wurde.
        self.voice_agent  = Agent(backend=voice_agent_backend, max_steps=8) if voice_agent_backend else self.agent

        self._search    = WebSearch()
        self._dashboard = DashboardReader()
        self._reminders = ReminderStore()

        self._proactive_tracker = ProactiveTracker()
        self._proactive_engine  = ProactiveEngine(llm=bg_llm, lzg=lzg, claude=None, kg=self.kg)

        self._last_user_ts = 0.0
        # Wird in start() gesetzt — Referenz auf den Haupt-Event-Loop, damit
        # lzg_embed aus Worker-Threads sicher hinein-schedulen kann.
        self._main_loop: asyncio.AbstractEventLoop | None = None

        self.autopilot = Autopilot(
            llm=bg_llm, lzg=lzg, dashboard=self._dashboard, reminders=self._reminders,
            channel=channel, proactive=self._proactive_engine,
            tracker=self._proactive_tracker, identity=IDENTITY,
            lock=None, is_user_active=self._user_active,
            search=self._search,
        )

        # ── Teilmodule ────────────────────────────────────────────────────────
        self.prompt_builder = PromptBuilder(
            embed_llm=embed_llm, lzg=lzg, kzg=self.kzg, kg=self.kg,
            reflection=self.reflection, dashboard=self._dashboard,
            forgetting=self.forgetting, identity=IDENTITY,
        )

        self.msg_handler = MessageHandler(
            kzg=self.kzg, lzg=lzg, agent=self.agent,
            prompt_builder=self.prompt_builder, channel=channel,
            proactive_tracker=self._proactive_tracker,
            forgetting=self.forgetting, extractor=self.extractor,
            bg_llm=bg_llm, alphaprogression=alphaprogression,
            on_user_active=self._mark_user_active,
        )

        self.idle_loop = IdleLoop(
            autopilot=self.autopilot, reflection=self.reflection,
            consolidator=self.consolidator, forgetting=self.forgetting,
            kzg=self.kzg, lzg=lzg, dashboard=self._dashboard,
            thermal=thermal, reminders=self._reminders,
            proactive_engine=self._proactive_engine,
            proactive_tracker=self._proactive_tracker,
            bg_llm=bg_llm, chat_llm=chat_llm, suggest_one=suggest_one,
            is_user_active=self._user_active,
            pattern_detector_mod=pattern_detector,
            generate_insight_task=generate_insight_task,
        )

    # ── User-Active State ─────────────────────────────────────────────────────

    def _user_active(self) -> bool:
        return (time.time() - self._last_user_ts) < 1800

    def _mark_user_active(self) -> None:
        self._last_user_ts = time.time()
        self.idle_loop.pause()

    # ── Öffentliche API (delegiert an Teilmodule) ─────────────────────────────

    async def handle_message(self, msg: IncomingMessage) -> None:
        await self.msg_handler.handle(msg, resume_idle_cb=self.idle_loop.resume)

    async def dashboard_respond(self, text: str, stream_cb=None) -> tuple[str, list]:
        return await self.msg_handler.dashboard_respond(text, stream_cb)

    async def voice_respond(self, text: str, stream_cb=None) -> tuple[str, list]:
        """Wie dashboard_respond, aber über das kleine, dauerhaft geladene
        Voice-Agent-Backend statt des großen Dashboard-Agenten (Latenz)."""
        return await self.msg_handler.dashboard_respond(text, stream_cb, agent=self.voice_agent)

    def lzg_embed(self, text: str) -> list[float]:
        """Synchroner Embedding-Wrapper für API-Endpoints.

        NUR aus Worker-Threads aufrufen (Threadpool-Endpoint oder asyncio.to_thread).
        Auf dem Event-Loop-Thread selbst würde das Blockieren auf dem Future den
        Loop einfrieren — dieser Fall wird abgefangen und liefert [] statt Freeze.
        """
        loop = self._main_loop
        if loop is None or not loop.is_running():
            log.warning("lzg_embed: kein laufender Event-Loop — Embedding übersprungen")
            return []
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # gut: wir sind in einem Worker-Thread
        else:
            log.warning("lzg_embed auf dem Event-Loop-Thread aufgerufen — übersprungen "
                        "(würde den Loop blockieren; Aufrufer muss asyncio.to_thread nutzen)")
            return []
        try:
            future = asyncio.run_coroutine_threadsafe(self.embed_llm.embed(text), loop)
            return future.result(timeout=10)
        except Exception as e:
            log.warning(f"lzg_embed fehlgeschlagen: {e}")
            return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    # Hält Referenzen auf Background-Tasks damit der GC sie nicht abräumt
    _bg_tasks: list[asyncio.Task]

    async def start(self) -> None:
        log.info("Orchestrator startet...")
        self._main_loop = asyncio.get_running_loop()
        self._bg_tasks = []
        self._register_services()
        await self._init_db()
        # Ab jetzt (DB bereit) WARNING/ERROR-Logs auch ins Fehler-Widget spiegeln.
        from core import log_observability
        log_observability.install()
        await self._init_skills()
        await self._restore_memory()
        await self._warmup()
        self.channel.on_message(self.handle_message)
        self.idle_loop.resume()
        self._bg_tasks.append(asyncio.create_task(self.idle_loop.monitoring_loop()))
        await self.channel.start()
        log_event("system", "Mantis gestartet")
        log.info("Mantis bereit ✅")

    def _register_services(self) -> None:
        """Kern-Services im Container registrieren, damit Domain-Module sie nutzen können."""
        services.register("lzg", self.lzg)
        services.register("kzg", self.kzg)
        services.register("kg", self.kg)
        services.register("chat_llm", self.chat_llm)
        services.register("bg_llm", self.bg_llm)
        services.register("embed_llm", self.embed_llm)
        services.register("channel", self.channel)
        services.register("dashboard", self._dashboard)
        log.debug(f"Services registriert: {services.registered()}")

    async def _init_db(self) -> None:
        """DB-Setup — Fehler bricht Start ab (kein Mantis ohne persistente Memory)."""
        try:
            self.lzg.setup()
            self._reminders.setup()
            db.run_migrations()
            # Verwaiste Foto-Analysen (Server während Hintergrund-Task neu gestartet) abräumen
            db.execute("UPDATE meals SET status='failed' WHERE status='analyzing'")
            log.info("Datenbank bereit")
        except Exception as e:
            log.error(f"DB-Fehler: {e}")
            raise  # Mantis ohne DB sinnlos

    async def _init_skills(self) -> None:
        """Skills binden + dynamische Skills aus vorherigen Sessions laden."""
        try:
            # In den Thread ausgelagert: refresh_health() macht bis zu 8 sequenzielle
            # COROS-MCP-Aufrufe (je eine LLM-Generierung serverseitig, 60s Timeout) —
            # synchron im Event-Loop wären das im schlechtesten Fall ~8 Minuten Blockade
            # beim Start.
            await asyncio.to_thread(self._dashboard.refresh_health)
        except Exception as e:
            log.debug(f"Health-Refresh beim Start: {e}")

        skills.bind(SkillContext(
            lzg=self.lzg, llm=self.bg_llm, search=self._search,
            reminders=self._reminders, dashboard=self._dashboard,
            channel=self.channel,
        ))
        try:
            load_all_on_startup()
        except Exception as e:
            log.error(f"Dynamische Skills konnten nicht geladen werden: {e}")

    async def _restore_memory(self) -> None:
        """KZG aus letzter Session wiederherstellen + Memory-Übersicht loggen."""
        try:
            past = self.lzg.load_recent_kzg(max_turns=config.KZG_MAX_TURNS)
            for t in past:
                self.kzg.add(t["role"], t["content"])
            if past:
                log.info(f"🔄 {len(past)} Turns wiederhergestellt")
                self.lzg.clear_kzg_sessions(keep_last=config.KZG_MAX_TURNS * 2)
        except Exception as e:
            log.warning(f"KZG-Wiederherstellung: {e}")
        try:
            mems = self.lzg.get_all(limit=20)
            if mems:
                log.info(f"🧠 {len(mems)} Erinnerungen, {len(skills.T.REGISTRY)} Tools")
        except Exception:
            pass

    async def _warmup(self) -> None:
        """LLM-Modelle aufwärmen — Tasks mit Referenz speichern (kein GC-Risiko)."""
        if isinstance(self.chat_llm, OllamaProvider):
            await self.chat_llm.pull_if_missing()
        self._bg_tasks += [
            asyncio.create_task(self.agent.warmup()),
            asyncio.create_task(fast.warmup()),
        ]

    async def stop(self) -> None:
        self.idle_loop.pause()
        for t in getattr(self, "_bg_tasks", []):
            t.cancel()
        for closer, label in [
            (self.lzg.close, "LZG"),
            (self._dashboard.close, "Dashboard"),
            (db.close_pool, "DB-Pool"),
        ]:
            try:
                closer()
            except Exception as e:
                log.debug(f"Stop/{label}: {e}")
        log_event("system", "Mantis gestoppt")
        log.info("Orchestrator gestoppt")
