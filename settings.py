"""
Typisierte Konfiguration via Pydantic BaseSettings.
Liest .env automatisch. Fehler werden beim Start sichtbar (nicht erst beim ersten Aufruf).

Verwendung:
    from settings import cfg
    print(cfg.OWNER_NAME)
"""
from __future__ import annotations
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MantisSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Dashboard ────────────────────────────────────────────────────────────
    DASHBOARD_HOST: str = "127.0.0.1"
    DASHBOARD_TOKEN: str = ""
    DASHBOARD_ALLOWED_ORIGINS: str = "http://localhost:1420,tauri://localhost,http://tauri.localhost,https://tauri.localhost"
    DASHBOARD_PORT: int = 7779

    # ── LLM ──────────────────────────────────────────────────────────────────
    OLLAMA_MODEL: str = "qwen3.5:9b"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_KEEP_ALIVE: str = "0"
    AGENT_MODEL_FAST: str = "qwen3.5:9b"
    # Interaktiver Haupt-Agent (Telegram/Dashboard-Chat). Benchmark 2026-07-09:
    # gemma4:e2b erreicht ~gleiche Chat-Qualität wie die 9B-Modelle, ist aber 3×
    # schneller — und teilt sich das Modell mit dem Voice-Agent (immer warm).
    AGENT_MODEL_STRONG: str = "gemma4:e2b"
    # Kleines, schnelles Modell für Ja/Nein-Klassifikation (z.B. Voice-Adress-Check).
    # Auf einem 24-Fälle-Testset verglichen: qwen3.5:2b (12/24), qwen2.5:3b-instruct
    # (16/24, ~1.1s), qwen3.5:4b (21/24, ~3.1s), qwen3.5:9b (21/24, ~6.0s),
    # gemma4:e2b (21/24, ~2.8s) — gemma4:e2b gewählt: gleiche Genauigkeit wie die
    # größeren Qwen-Modelle, aber am schnellsten davon.
    ADDRESS_CHECK_MODEL: str = "gemma4:e2b"
    # Dediziertes, dauerhaft geladenes Modell für Voice-Antworten (nicht der große
    # Dashboard-Agent) — gleiches Modell wie ADDRESS_CHECK_MODEL, da es schon für
    # den Adress-Check läuft (siehe Kommentar oben) und laut Latenztest vom
    # 2026-07-05 warm ~5.5-7.5s statt 166s (qwen3.5:9b) braucht. KEEP_ALIVE="-1"
    # hält es dauerhaft im Speicher, statt es (wie der Haupt-Agent) nach jedem
    # Call sofort wieder zu entladen (OLLAMA_KEEP_ALIVE="0").
    VOICE_AGENT_MODEL: str = "gemma4:e2b"
    VOICE_AGENT_KEEP_ALIVE: str = "-1"
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_CHAT_MODEL: str = "claude-haiku-4-5-20251001"
    # Voll-lokaler Betrieb: ignoriert den ANTHROPIC_API_KEY und nutzt überall Ollama
    # (kein Cloud-Call, keine Claude-Fallbacks). Der Key bleibt für Tools/Benchmarks nutzbar.
    LLM_LOCAL_ONLY: bool = False
    # ── Background-LLM Routing ────────────────────────────────────────────────
    # Default: qwen3.5:9b für Memory, Review, Konsolidierung
    BG_DEFAULT_MODEL: str = ""          # leer = OLLAMA_MODEL
    # Reasoning-Spezialist für Briefings, Insights, Pattern-Detection.
    # Benchmark 2026-07-09: qwen3.5:9b (8.4/10 @ 7s) schlug deepseek-r1:14b sowohl
    # ohne (6.6 @ 70s) als auch MIT Thinking (7.2 @ 37s) — bei 4-5× weniger Latenz.
    BG_REASONING_MODEL: str = "qwen3.5:9b"
    # Code-Spezialist des Background-LLM. Benchmark 2026-07-09: ornith:9b war
    # der beste Coder im Feld (8.7/10) und schneller als qwen3.5.
    # ACHTUNG, der frühere Kommentar "für create_skill / Skill-Factory" war
    # falsch (geprüft am 09.09.2026 am laufenden System): create_skill bekommt
    # `source_code` als Tool-Argument vom CHAT-Agenten (AGENT_MODEL_STRONG), und
    # die Forge fährt über die claude-CLI. Dieses Modell greift ausschließlich
    # dort, wo ein Background-Prompt die _CODE_KEYWORDS in llm/routed.py trifft
    # (background_review, Konsolidierung, idle_loop) — nicht in der Skill-Factory.
    # Ersetzt 2026-09-09 durch Ornith-1.5 (Benchmark-Lauf bench/nanbeige42,
    # n=12 — die Unterlagen liegen nur lokal und bewusst nicht im Repo, weil
    # die COROS-Aufgabe echte Messwerte enthaelt):
    # gleiche Code-Trefferquote (11/12), aber halbe Latenz (Median 6.3s statt
    # 14.3s im Code-Block). NICHT das nackte ornith-1.5:9b eintragen — dessen
    # Modelfile bringt weder SYSTEM noch Sampling-Parameter mit, was allein
    # 11/12 auf 8/12 drückt. Der lokale Tag ornith-1.5:9b-sys backt beides aus
    # ornith:9b ein und ist damit ein echter Drop-in (verifiziert, 11/12).
    # Neu bauen: siehe ORNITH-1.5.md §8 in den lokalen Unterlagen.
    BG_CODE_MODEL: str = "ornith-1.5:9b-sys"
    # Sampling-Temperatur für die Code-Route. Der Ollama-Pfad sendet temperature
    # immer explizit und überschreibt damit den Wert aus dem Modelfile — ohne
    # diese Einstellung liefe der Coder auf dem generischen 0.7-Default.
    # Messung 09.09.2026 (Code-Aufgabe des Benchmarks): 0.6 → 8/8, 0.7 → 7/8.
    BG_CODE_TEMPERATURE: float = 0.6
    # Vision-Modell für Foto-Analyse (Mahlzeiten)
    VISION_MODEL: str = "qwen3-vl:8b"

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_IDS_RAW: str = Field("", alias="TELEGRAM_ALLOWED_IDS")

    # Shared-Secret für externe Trigger (Not-Stopp, Automationen) über /api/trigger/*.
    # Leer = Trigger-API deaktiviert (sicherer Default).
    TRIGGER_TOKEN: str = ""

    # ── Besitzer ─────────────────────────────────────────────────────────────
    OWNER_NAME: str = ""
    OWNER_EMAIL: str = ""
    OWNER_TIMEZONE: str = "Europe/Berlin"

    # ── Datenquellen ─────────────────────────────────────────────────────────
    HEALTH_API_URL: str = ""
    CALENDAR_ICS_URLS: str = ""

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://localhost:5432/mantis"

    # ── Web Push (VAPID) ─────────────────────────────────────────────────────
    VAPID_PRIVATE_KEY_PATH: str = "data/vapid_private.pem"
    VAPID_PUBLIC_KEY: str = ""
    VAPID_CLAIM_EMAIL: str = ""

    # ── Search ───────────────────────────────────────────────────────────────
    BRAVE_API_KEY: str = ""

    # ── Spotify (nur für die Suche in „spiel [X]"; Playback läuft lokal) ─────
    # Kostenlose App auf developer.spotify.com anlegen; leer = Suche deaktiviert,
    # Play/Pause/Volume/Status funktionieren trotzdem (reines AppleScript).
    SPOTIFY_CLIENT_ID: str = ""
    SPOTIFY_CLIENT_SECRET: str = ""

    # ── Google Calendar ───────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_CALENDAR_ID: str = "primary"

    # ── Thermal ──────────────────────────────────────────────────────────────
    THERMAL_TARGET_CELSIUS: float = 70.0
    THERMAL_MAX_CELSIUS: float = 85.0

    # ── Idle Loop ────────────────────────────────────────────────────────────
    IDLE_MIN_SLEEP_S: int = 2
    IDLE_MAX_SLEEP_S: int = 60
    IDLE_EVAL_AFTER_S: int = 600

    # ── Proaktiv ─────────────────────────────────────────────────────────────
    PROACTIVE_WAIT_AFTER_CONV: int = 600
    PROACTIVE_INTERVAL: int = 5400

    # ── Accountability & Momentum (v0: Daily Anchor + Der Block) ──────────────
    # Ein Nutzer (Timo), Config als simple ENV-Werte. Uhrzeiten im Format "HH:MM"
    # in lokaler Serverzeit (= OWNER_TIMEZONE). Der Block ist EIN Deep-Work-Fenster
    # pro Tag; das Blockende ergibt sich aus Start + Dauer.
    ACCOUNTABILITY_ENABLED: bool = True
    ACCOUNTABILITY_ANCHOR_TIME: str = "08:00"    # Morgen-Anker ("Was ist das EINE Ding?")
    ACCOUNTABILITY_BLOCK_START: str = "10:00"    # Blockstart ("Handy weg.")
    ACCOUNTABILITY_BLOCK_MINUTES: int = 90       # Blockdauer (60–90 min sinnvoll)

    # ── Memory ───────────────────────────────────────────────────────────────
    LZG_EMBED_MODEL: str = "qwen3-embedding:0.6b"
    LZG_TOP_K: int = 5
    KZG_MAX_TURNS: int = 20

    # ── Computed ─────────────────────────────────────────────────────────────

    @property
    def TELEGRAM_ALLOWED_IDS(self) -> set[str]:
        raw = {s.strip() for s in self.TELEGRAM_ALLOWED_IDS_RAW.split(",") if s.strip()}
        if self.TELEGRAM_CHAT_ID:
            raw.add(self.TELEGRAM_CHAT_ID)
        return raw

    @property
    def VAPID_CLAIM_EMAIL_RESOLVED(self) -> str:
        return self.VAPID_CLAIM_EMAIL or self.OWNER_EMAIL or "admin@localhost"

    # ── Validation ───────────────────────────────────────────────────────────

    @field_validator("OWNER_NAME")
    @classmethod
    def warn_owner_name(cls, v: str) -> str:
        if not v:
            import logging
            logging.getLogger("mantis.settings").warning(
                "⚠️  OWNER_NAME nicht in .env gesetzt – bitte eintragen"
            )
        return v

    @field_validator("ACCOUNTABILITY_ANCHOR_TIME", "ACCOUNTABILITY_BLOCK_START")
    @classmethod
    def valid_hhmm(cls, v: str) -> str:
        try:
            h, m = v.strip().split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError
        except Exception:
            raise ValueError(f"Ungültige Uhrzeit '{v}' – erwartet HH:MM (z.B. 08:00)")
        return v.strip()

    @field_validator("THERMAL_TARGET_CELSIUS", "THERMAL_MAX_CELSIUS")
    @classmethod
    def celsius_range(cls, v: float) -> float:
        if not (30.0 <= v <= 120.0):
            raise ValueError(f"Temperaturwert {v}°C außerhalb sinnvollem Bereich (30–120)")
        return v

    @model_validator(mode="after")
    def thermal_order(self) -> "MantisSettings":
        if self.THERMAL_TARGET_CELSIUS >= self.THERMAL_MAX_CELSIUS:
            raise ValueError(
                f"THERMAL_TARGET_CELSIUS ({self.THERMAL_TARGET_CELSIUS}) muss kleiner als "
                f"THERMAL_MAX_CELSIUS ({self.THERMAL_MAX_CELSIUS}) sein"
            )
        return self


cfg = MantisSettings()
