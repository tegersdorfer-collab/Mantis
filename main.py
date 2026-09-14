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
    from llm.local import OllamaProvider
    from memory.lzg import LZG
    from communication.telegram import TelegramChannel
    from thermal import ThermalMonitor
    from orchestrator import Orchestrator

    # Voll-lokaler Betrieb (LLM_LOCAL_ONLY) ignoriert den API-Key komplett.
    use_claude = bool(cfg.ANTHROPIC_API_KEY) and not cfg.LLM_LOCAL_ONLY
    if cfg.LLM_LOCAL_ONLY:
        log.info("🔒 LLM_LOCAL_ONLY: alle LLMs laufen lokal (Claude deaktiviert)")

    # ── Chat-LLM ────────────────────────────────────────────────────────────
    if use_claude:
        from llm.claude import ClaudeProvider
        chat_llm = ClaudeProvider(model=cfg.CLAUDE_CHAT_MODEL)
        log.info(f"💬 Chat-LLM: {cfg.CLAUDE_CHAT_MODEL}")
    else:
        chat_llm = OllamaProvider()
        log.info(f"💬 Chat-LLM: Ollama {cfg.OLLAMA_MODEL} (lokal)")

    # ── Agent-Backend: interaktiver Haupt-Agent (Telegram/Dashboard-Chat) ────────
    # Lokal: gemma4:e2b (AGENT_MODEL_STRONG) — laut Benchmark ~9B-Chatqualität bei 3×
    # Speed. keep_alive="-1" hält es dauerhaft im RAM; da der Voice-Agent dasselbe
    # Modell nutzt, ist gemma immer warm → keine Cold-Reload-Latenz mehr.
    from core.backends.ollama import OllamaBackend
    if use_claude:
        from core.backends.claude import ClaudeBackend
        from core.backends.fallback import FallbackBackend
        agent_backend = FallbackBackend(
            primary=ClaudeBackend(model=cfg.CLAUDE_CHAT_MODEL),
            fallback=OllamaBackend(),
        )
        log.info(f"🔧 Agent-Backend: Claude {cfg.CLAUDE_CHAT_MODEL} → Fallback Ollama {cfg.AGENT_MODEL_STRONG} (lokal)")
    else:
        agent_backend = OllamaBackend(model=cfg.AGENT_MODEL_STRONG, keep_alive="-1")
        log.info(f"🔧 Agent-Backend: Ollama {cfg.AGENT_MODEL_STRONG} (lokal, dauerhaft geladen)")

    # ── Voice-Agent-Backend: kleines, dauerhaft geladenes Modell ────────────────
    if use_claude:
        from core.backends.fallback import FallbackBackend
        from core.backends.claude import ClaudeBackend
        voice_agent_backend = FallbackBackend(
            primary=OllamaBackend(model=cfg.VOICE_AGENT_MODEL, keep_alive=cfg.VOICE_AGENT_KEEP_ALIVE),
            fallback=ClaudeBackend(model=cfg.CLAUDE_CHAT_MODEL),
        )
        log.info(f"🎙️  Voice-Agent-Backend: Ollama {cfg.VOICE_AGENT_MODEL} (dauerhaft) → Fallback {cfg.CLAUDE_CHAT_MODEL}")
    else:
        voice_agent_backend = OllamaBackend(model=cfg.VOICE_AGENT_MODEL, keep_alive=cfg.VOICE_AGENT_KEEP_ALIVE)
        log.info(f"🎙️  Voice-Agent-Backend: Ollama {cfg.VOICE_AGENT_MODEL} (lokal, dauerhaft geladen)")

    # ── Background-LLM: Routed (Spezialisten je nach Aufgabe) ─────────────────
    from llm.routed import RoutedLLMProvider
    bg_llm = RoutedLLMProvider()
    log.info(f"🧠 Background-LLM: Routed (default={cfg.BG_DEFAULT_MODEL} | reasoning={cfg.BG_REASONING_MODEL} | code={cfg.BG_CODE_MODEL})")

    # ── Embed-LLM: immer Ollama (kein Embedding via Claude) ───────────────────
    embed_llm = OllamaProvider()

    lzg     = LZG()
    thermal = ThermalMonitor()
    channel = TelegramChannel()
    orchestrator = Orchestrator(
        chat_llm=chat_llm, bg_llm=bg_llm, embed_llm=embed_llm,
        agent_backend=agent_backend, voice_agent_backend=voice_agent_backend,
        channel=channel, lzg=lzg, thermal=thermal,
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

    async def shutdown():
        log.info("Fährt herunter...")
        await orchestrator.stop()
        await channel.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await orchestrator.start()
    asyncio.create_task(api_server.serve())
    log.info(f"🖥️  Dashboard läuft auf http://{dashboard_host}:{cfg.DASHBOARD_PORT} (bindet auf {dashboard_host})")
    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())
