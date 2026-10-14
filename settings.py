"""
Typisierte Konfiguration via Pydantic BaseSettings.
Liest .env automatisch. Fehler werden beim Start sichtbar (nicht erst beim ersten Aufruf).

Verwendung:
    from settings import cfg
    print(cfg.OWNER_NAME)
"""
from __future__ import annotations
import json

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

    # ── Gym-Tracker ─────────────────────────────────────────────────────────
    GYM_ENABLED: bool = True
    GYM_HOST: str = "127.0.0.1"
    GYM_PORT: int = 7782
    GYM_SESSION_DAYS: int = 180

    # ── LLM ──────────────────────────────────────────────────────────────────
    # Ollama wird ausschließlich für den lokalen Embedding-Pfad genutzt.
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_NUM_CTX: int = 8192
    OLLAMA_NUM_CTX_OVERRIDES: str = ""
    VOICE_MAX_TOKENS: int = 500
    VOICE_SILENCE_MS: int = 800
    VOICE_ADAPTIVE_SILENCE: bool = True
    VOICE_SILENCE_FAST_MS: int = Field(default=450, gt=0)
    VOICE_SILENCE_SLOW_MS: int = Field(default=1200, gt=0)
    VOICE_PARTIAL_STT_MS: int = Field(default=700, gt=0)
    VOICE_PARTIAL_STALE_MS: int = Field(default=400, gt=0)
    VOICE_PARTIAL_MAX_MS: int = Field(default=600, gt=0)
    VOICE_FILLERS_ENABLED: bool = True
    VOICE_FILLER_ACK_DELAY_MS: int = 800
    VOICE_FILLERS_ACK: list[str] = Field(
        default_factory=lambda: ["Mhm.", "Okay.", "Moment."],
    )
    VOICE_FILLERS_TOOL: list[str] = Field(
        default_factory=lambda: [
            "Ich schau kurz nach.",
            "Moment, ich check das.",
            "Sekunde, ich schau nach.",
        ],
    )
    # ── STT (Sprache → Text) ─────────────────────────────────────────────────
    # "parakeet" (Default seit 22.09.2026) oder "whisper" (whisper.cpp medium, der
    # vorherige Stand — bleibt als Fallback, wenn parakeet-mlx fehlt).
    # Benchmark 22.09.2026 auf den 36 ECHTEN Aufnahmen aus data/wakeword/samples
    # (Timos Stimme, echtes Mikro) — entscheidend war der Weckname, nicht der WER:
    #   Parakeet v3   Weckname 72 %, 0,17 s/Clip
    #   whisper medium  ... 28 %, 0,78 s/Clip   ("Mentos", "Mentis", "Ventus")
    #   whisper turbo   ... 22 %, 1,06 s/Clip
    # Ganze Sätze transkribieren alle drei fehlerfrei; der Unterschied liegt bei
    # kurzen Äußerungen und Eigennamen — genau dem Voice-Alltag.
    # TTS: Cartesia Sonic 3.6; bei Fehlern fällt Mantis lokal auf Kokoro/Piper zurück.
    TTS_ENGINE: str = "cartesia"
    TTS_KOKORO_VOICE: str = "dm_martin"
    CARTESIA_API_KEY: str = ""
    TTS_CARTESIA_MODEL: str = "sonic-3.6"
    TTS_CARTESIA_VOICE: str = "afa425cf-5489-4a09-8a3f-d3cb1f82150d"
    TTS_CARTESIA_LANGUAGE: str = "de"
    # Lautschreibung für Wörter, die die deutsche Stimme falsch ausspricht (Timo, 14.10.2026: Mantis englisch).
    TTS_CARTESIA_PRONUNCIATIONS: dict[str, str] = {"Mantis": "Mäntis"}
    TTS_CARTESIA_USD_PER_MILLION_CHARS: float = 65.0  # Pro-Überschuss, 1 Zeichen = 1 Credit
    STT_ENGINE: str = "parakeet"
    STT_PARAKEET_MODEL: str = "mlx-community/parakeet-tdt-0.6b-v3"
    STT_PRELOAD: bool = True
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_ALLOWED_MODELS: list[str] = Field(
        default_factory=lambda: ["claude-haiku-5-5"],
    )
    ANTHROPIC_FAST_THINKING_DISABLED: bool = True
    ANTHROPIC_FAST_EFFORT: str = "low"
    CLAUDE_CHAT_MODEL: str = "claude-haiku-5-5"
    CHAT_ANTHROPIC_MODEL: str = "claude-haiku-5-5"
    CHAT_CLOUD_MODEL: str = "openai/gpt-6-luna"
    CHAT_CLOUD_FALLBACK_MODEL: str = ""
    CHAT_CLOUD_REASONING_EFFORT: str = "low"
    CHAT_CLOUD_TIMEOUT_S: float = 30.0
    FAST_CHAIN_ORDER: list[str] = Field(
        default_factory=lambda: ["openrouter", "anthropic", "chatgpt-plan"],
    )
    VOICE_CHAIN_ORDER: list[str] = Field(
        default_factory=lambda: ["openrouter", "anthropic", "chatgpt-plan"],
    )
    CHAT_CHATGPT_PLAN_ENABLED: bool = True
    CHAT_CHATGPT_PLAN_MODEL: str = "gpt-6-luna"
    CHATGPT_AUTH_FILE: str = "~/.config/mantis/chatgpt_auth.json"
    CHATGPT_AUTH_CALLBACK_PORT: int = 1456
    # Legacy-Flag bleibt für bestehende .env-Dateien; alle LLM-Aufrufe bleiben Cloud.
    LLM_LOCAL_ONLY: bool = False
    # ── Jev-Integrationen ───────────────────────────────────────────────────
    JEV_ENABLED: bool = False
    JEV_FORGE_GATE_ENABLED: bool = False
    JEV_PROVIDER: str = "typesafe"
    JEV_PROVIDER_CHAIN: str = ""
    TYPESAFE_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    JEV_MODEL: str = "jev-latest"
    CLEF_MODEL: str = "clef-flash"
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_AUTH_TOKEN: str = ""
    JEV_TIMEOUT_S: float = 3.0
    JEV_COOLDOWN_S: float = 120.0
    JEV_UI_ACT_CONFIDENCE: float = 0.55
    JEV_LOG_PATH: str = ""
    # Sampling-Temperatur für codeartige Background-Aufgaben.
    BG_CODE_TEMPERATURE: float = 0.6

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_IDS_RAW: str = Field("", alias="TELEGRAM_ALLOWED_IDS")

    # ── Discord ─────────────────────────────────────────────────────────────
    DISCORD_ENABLED: bool = False
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_ALLOWED_USER_IDS_RAW: str = Field("", alias="DISCORD_ALLOWED_USER_IDS")
    DISCORD_CHANNEL_ID: str = ""
    NOTIFY_CHANNEL: str = "telegram"

    # Modellfreier Discord-DM-Fast-Path für abgekoppelte codex-task-Prozesse.
    # Leere/ungültige JSON-Allowlist und Feature aus bleiben fail closed.
    AGENT_REMOTE_ENABLED: bool = False
    AGENT_REMOTE_REPOS_RAW: str = Field('{"mantis":"~/Mantis"}', alias="AGENT_REMOTE_REPOS")
    CODEX_TASK_MODEL: str = "gpt-6-luna"
    CODEX_TASK_EFFORT: str = "max"

    # Shared-Secret für externe Trigger (Not-Stopp, Automationen) über /api/trigger/*.
    # Leer = Trigger-API deaktiviert (sicherer Default).
    TRIGGER_TOKEN: str = ""

    # ── Besitzer ─────────────────────────────────────────────────────────────
    OWNER_NAME: str = ""
    OWNER_EMAIL: str = ""
    OWNER_TIMEZONE: str = "Europe/Berlin"

    # ── Datenquellen ─────────────────────────────────────────────────────────
    CALENDAR_ICS_URLS: str = ""

    # ── COROS ────────────────────────────────────────────────────────────────
    # Offizielles COROS-MCP. EU-Route als Default; mcpus/mcpcn falls der Account
    # dort liegt. Siehe docs/coros-setup.md.
    COROS_MCP_URL: str = "https://mcpeu.coros.com/mcp"

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://localhost:5432/mantis"

    # ── Web Push (VAPID) ─────────────────────────────────────────────────────
    VAPID_PRIVATE_KEY_PATH: str = "data/vapid_private.pem"
    VAPID_PUBLIC_KEY: str = ""
    VAPID_CLAIM_EMAIL: str = ""

    # ── Search ───────────────────────────────────────────────────────────────
    BRAVE_API_KEY: str = ""

    # ── Spotify (Web-API-User-OAuth; Credentials der Developer-App) ─────────
    # Für Suche und Premium-Playback. Der einmalige Login speichert den User-
    # Token in data/spotify_token.json; leer bleibt der Legacy-Fallback aktiv.
    SPOTIFY_CLIENT_ID: str = ""
    SPOTIFY_CLIENT_SECRET: str = ""
    # Optionaler exakter Spotify-Gerätename; leer = aktives Gerät, sonst Computer.
    SPOTIFY_DEVICE_NAME: str = ""

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
    CHAT_TTL_DAYS: int = 365

    # ── Hintergrundjobs ──────────────────────────────────────────────────────
    SITUATION_ENABLED: bool = True
    SITUATION_REFRESH_S: int = 300
    INSIGHTS_ENABLED: bool = False
    PATTERN_DETECTION_ENABLED: bool = False
    TRAINING_PLAN_AUTOGEN_ENABLED: bool = False
    NEWS_REFRESH_ENABLED: bool = False
    DAILY_REFLECTION_ENABLED: bool = False
    MEMORY_CONSOLIDATION_ENABLED: bool = False
    TASK_DEEP_EXECUTION_ENABLED: bool = False
    BACKGROUND_REVIEW_ENABLED: bool = False

    # ── Proaktiv ─────────────────────────────────────────────────────────────
    # Autonome Telegram-/Push-Nachrichten sind bewusst aus, bis Timo sie wieder
    # ausdrücklich nutzen möchte. Direkte Antworten auf eigene Nachrichten bleiben aktiv.
    AUTONOMOUS_MESSAGES_ENABLED: bool = False
    PROACTIVE_WAIT_AFTER_CONV: int = 600
    PROACTIVE_INTERVAL: int = 5400

    # ── Accountability & Momentum (v0: Daily Anchor + Der Block) ──────────────
    # Ein Nutzer (Timo), Config als simple ENV-Werte. Uhrzeiten im Format "HH:MM"
    # in lokaler Serverzeit (= OWNER_TIMEZONE). Der Block ist EIN Deep-Work-Fenster
    # pro Tag; das Blockende ergibt sich aus Start + Dauer.
    ACCOUNTABILITY_ENABLED: bool = False
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
    def DISCORD_ALLOWED_USER_IDS(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            value.strip()
            for value in self.DISCORD_ALLOWED_USER_IDS_RAW.split(",")
            if value.strip()
        ))

    @property
    def AGENT_REMOTE_REPOS(self) -> str:
        return self.AGENT_REMOTE_REPOS_RAW

    @property
    def VAPID_CLAIM_EMAIL_RESOLVED(self) -> str:
        return self.VAPID_CLAIM_EMAIL or self.OWNER_EMAIL or "admin@localhost"

    # ── Validation ───────────────────────────────────────────────────────────

    @field_validator("OLLAMA_NUM_CTX")
    @classmethod
    def positive_ollama_num_ctx(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("OLLAMA_NUM_CTX muss positiv sein")
        return v

    @field_validator("VOICE_SILENCE_MS")
    @classmethod
    def positive_voice_silence_ms(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("VOICE_SILENCE_MS muss positiv sein")
        return value

    @field_validator("FAST_CHAIN_ORDER", "VOICE_CHAIN_ORDER")
    @classmethod
    def valid_cloud_chain_order(cls, value: list[str]) -> list[str]:
        allowed = {"openrouter", "anthropic", "chatgpt-plan"}
        normalized = [str(stage).strip().casefold() for stage in value]
        if not normalized or any(stage not in allowed for stage in normalized):
            raise ValueError(
                "Cloud-Kettenreihenfolge darf nur openrouter, anthropic und chatgpt-plan enthalten"
            )
        if len(set(normalized)) != len(normalized):
            raise ValueError("Cloud-Kettenreihenfolge darf keine Stufe doppelt enthalten")
        return normalized

    @field_validator("OLLAMA_NUM_CTX_OVERRIDES")
    @classmethod
    def valid_ollama_num_ctx_overrides(cls, v: str) -> str:
        parsed: dict[str, int] = {}
        for raw_entry in v.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            if entry.count("=") != 1:
                raise ValueError(
                    f"Ungültiger OLLAMA_NUM_CTX_OVERRIDES-Eintrag '{entry}' – erwartet Modell=Kontext"
                )
            model, raw_ctx = (part.strip() for part in entry.split("=", 1))
            if not model or not raw_ctx:
                raise ValueError(
                    f"Ungültiger OLLAMA_NUM_CTX_OVERRIDES-Eintrag '{entry}' – Modell und Kontext sind erforderlich"
                )
            try:
                num_ctx = int(raw_ctx)
            except ValueError as e:
                raise ValueError(
                    f"Ungültiger Kontext '{raw_ctx}' für Modell '{model}' – erwartet eine positive Ganzzahl"
                ) from e
            if num_ctx <= 0:
                raise ValueError(f"Kontext für Modell '{model}' muss positiv sein")
            if model in parsed:
                raise ValueError(f"Doppelter Modell-Override für '{model}'")
            parsed[model] = num_ctx
        return ",".join(f"{model}={num_ctx}" for model, num_ctx in parsed.items())

    @field_validator("TTS_CARTESIA_PRONUNCIATIONS", mode="before")
    @classmethod
    def merge_cartesia_pronunciations(cls, value):
        defaults = {"Mantis": "Mäntis"}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("TTS_CARTESIA_PRONUNCIATIONS muss gültiges JSON sein") from exc
        if value is None:
            return defaults
        if not isinstance(value, dict):
            raise ValueError("TTS_CARTESIA_PRONUNCIATIONS muss ein JSON-Objekt sein")
        return {**defaults, **value}

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

    @field_validator("DISCORD_BOT_TOKEN")
    @classmethod
    def strip_discord_token(cls, v: str) -> str:
        return v.strip()

    @field_validator("DISCORD_CHANNEL_ID")
    @classmethod
    def strip_discord_channel_id(cls, v: str) -> str:
        return v.strip()

    @field_validator("NOTIFY_CHANNEL")
    @classmethod
    def valid_notify_channel(cls, v: str) -> str:
        channel = v.strip().lower()
        if channel not in {"telegram", "discord"}:
            raise ValueError("NOTIFY_CHANNEL muss 'telegram' oder 'discord' sein")
        return channel

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
