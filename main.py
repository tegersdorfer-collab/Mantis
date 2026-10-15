"""
Mantis – Entry Point
"""
import asyncio
import logging
import os
import signal
import sys

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# httpx/httpcore log request URLs at INFO. Telegram Bot API URLs contain the
# bot token, so HTTP client request logging must stay above INFO globally.
for _http_logger_name in ("httpx", "httpcore"):
    logging.getLogger(_http_logger_name).setLevel(logging.WARNING)
log = logging.getLogger("mantis")


def ensure_single_instance():
    pidfile = "/tmp/mantis.pid"
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log.error(f"Mantis läuft bereits (PID {old_pid}). Beende mit: kill {old_pid}")
            sys.exit(1)
        except (ProcessLookupError, ValueError, OSError):
            pass  # Alter Prozess tot
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.unlink(pidfile) if os.path.exists(pidfile) else None)


def validate_dashboard_host(host: str) -> str:
    """Accept only explicit loopback or Tailscale addresses; never wildcard/LAN."""
    import ipaddress
    address = ipaddress.ip_address(host)
    tailnets = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
    if not address.is_loopback and not any(address in network for network in tailnets):
        raise ValueError("DASHBOARD_HOST must be a loopback or explicit Tailscale IP")
    return str(address)


async def _stop_components(components) -> None:
    """Stoppt Komponenten einzeln, damit ein Fehler den Rest nicht blockiert."""
    for name, component in components:
        try:
            stop = component if callable(component) else component.stop
            await stop()
        except Exception as exc:
            log.warning("%s konnte nicht sauber beendet werden (%s)", name, type(exc).__name__)


async def _shutdown_services(components, stop_tts_worker, stop_gym_server, stop_event) -> None:
    """Beendet App-Dienste, den isolierten TTS-Worker und den Gym-Server."""
    try:
        await _stop_components((*components, ("Klon-TTS", stop_tts_worker)))
    finally:
        try:
            await stop_gym_server()
        finally:
            stop_event.set()


async def main():
    ensure_single_instance()

    print("""
    ╔═══════════════════════════════╗
    ║         A L F R E D           ║
    ║   Persönlicher AI-Begleiter   ║
    ╚═══════════════════════════════╝
    """)

    if not __import__("config").TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN nicht gesetzt in .env")
        sys.exit(1)

    import config as cfg
    from llm.local import OllamaEmbeddingProvider
    from memory.lzg import LZG
    from communication.telegram import TelegramChannel
    from thermal import ThermalMonitor
    from orchestrator import Orchestrator

    from core.backends.factory import (
        CloudChainUnavailableError,
        build_chat_backend,
        build_chat_task_provider,
        cloud_chain,
        describe_backend_chain,
        warn_if_local_only,
    )
    from core.backends.llm_adapter import BackendLLMAdapter
    from core import fast, vision

    warn_if_local_only(cfg)

    # ── Haupt-Chat: gleiche bestehende Cloud-Kette wie der Agent ──────────────
    agent_backend = build_chat_backend(cfg)
    chat_llm = BackendLLMAdapter(agent_backend)
    log.info("LLM-Kette [chat]: %s", describe_backend_chain(agent_backend))

    # ── Voice, Fast und Vision nutzen feste Varianten derselben Stufen ────────
    voice_agent_backend = cloud_chain("voice", reasoning="low", timeout=cfg.CHAT_CLOUD_TIMEOUT_S, cfg=cfg)
    log.info("LLM-Kette [voice]: %s", describe_backend_chain(voice_agent_backend))
    log.info("LLM-Kette [fast]: %s", describe_backend_chain(fast.get_backend_chain()))
    try:
        vision_backend = vision.get_backend_chain()
    except CloudChainUnavailableError:
        log.warning("LLM-Kette [vision]: kein bildfähiger Cloud-Anbieter konfiguriert")
    else:
        log.info("LLM-Kette [vision]: %s", describe_backend_chain(vision_backend))

    # ── Background-LLM: dieselbe Cloud-Kette, Sampling je Aufgabenart ─────────
    from llm.routed import RoutedLLMProvider
    bg_llm = RoutedLLMProvider()
    log.info("LLM-Kette [background]: %s", describe_backend_chain(bg_llm._provider.backend))

    chat_task_llm = build_chat_task_provider(cfg)
    log.info("LLM-Kette [background-chat]: %s", describe_backend_chain(chat_task_llm.backend))

    # ── Embeddings bleiben lokal bei Ollama ──────────────────────────────────
    embed_llm = OllamaEmbeddingProvider()

    lzg     = LZG()
    thermal = ThermalMonitor()
    channel = TelegramChannel()
    additional_channels = {}
    if cfg.DISCORD_ENABLED:
        from communication.discord import DiscordChannel
        additional_channels["discord"] = DiscordChannel()
    else:
        log.info("Discord-Kanal inaktiv (DISCORD_ENABLED=False)")
    orchestrator = Orchestrator(
        chat_llm=chat_llm, bg_llm=bg_llm, embed_llm=embed_llm,
        agent_backend=agent_backend, voice_agent_backend=voice_agent_backend,
        chat_task_llm=chat_task_llm,
        channel=channel, lzg=lzg, thermal=thermal,
        additional_channels=additional_channels,
    )

    # Default is loopback; remote access must use an explicit Tailnet address.
    import uvicorn
    from web.api import create_app
    dashboard_host = validate_dashboard_host(cfg.DASHBOARD_HOST)

    # SKILL.md Index beim Start laden
    from core import skill_md as _skill_md
    n_skills = _skill_md.scan_all()
    if n_skills:
        log.info(f"📘 {n_skills} Skill-Prozedur(en) geladen")

    api_app = create_app(orchestrator)
    api_conf = uvicorn.Config(api_app, host=dashboard_host, port=cfg.DASHBOARD_PORT, log_level="warning")
    api_server = uvicorn.Server(api_conf)

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    gym_server_task: asyncio.Task[None] | None = None

    async def stop_gym_server():
        if gym_server_task is None or gym_server_task.done():
            return
        if not gym_server_task.cancelling():
            gym_server_task.cancel()
        try:
            await gym_server_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Fehler beim Beenden des Gym-Servers")

    async def shutdown():
        log.info("Fährt herunter...")
        from core.tts import _stop_klon_worker

        await _shutdown_services(
            (("Orchestrator", orchestrator), ("Telegram-Kanal", channel)),
            _stop_klon_worker,
            stop_gym_server,
            stop_event,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await orchestrator.start()
    from core.voice import schedule_parakeet_preload
    preload_task = schedule_parakeet_preload()
    if preload_task is not None:
        orchestrator._bg_tasks.append(preload_task)
    from core.voice import schedule_parakeet_keepwarm
    keepwarm_task = schedule_parakeet_keepwarm(preload_task)
    if keepwarm_task is not None:
        orchestrator._bg_tasks.append(keepwarm_task)
    from core.voice_fillers import schedule_voice_fillers_preload
    fillers_task = schedule_voice_fillers_preload(preload_task)
    if fillers_task is not None:
        orchestrator._bg_tasks.append(fillers_task)
    asyncio.create_task(api_server.serve())
    log.info(f"🖥️  Dashboard läuft auf http://{dashboard_host}:{cfg.DASHBOARD_PORT} (bindet auf {dashboard_host})")
    if cfg.GYM_ENABLED:
        from web.gym_server import run_gym_server
        gym_server_task = asyncio.create_task(run_gym_server(cfg.GYM_HOST, cfg.GYM_PORT))
        log.info(f"🏋️ Gym-App: http://{cfg.GYM_HOST}:{cfg.GYM_PORT}")
    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())
