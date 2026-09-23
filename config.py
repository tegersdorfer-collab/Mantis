"""
Backward-kompatibler Konfigurations-Wrapper.
Alle Module importieren weiterhin `from config import X` — die Werte
kommen jetzt aber aus dem typisierten `settings.cfg` (Pydantic BaseSettings).
"""
from settings import cfg

DASHBOARD_HOST           = cfg.DASHBOARD_HOST
DASHBOARD_TOKEN          = cfg.DASHBOARD_TOKEN
DASHBOARD_ALLOWED_ORIGINS = cfg.DASHBOARD_ALLOWED_ORIGINS
DASHBOARD_PORT           = cfg.DASHBOARD_PORT

OLLAMA_MODEL             = cfg.OLLAMA_MODEL
OLLAMA_BASE_URL          = cfg.OLLAMA_BASE_URL
OLLAMA_KEEP_ALIVE        = cfg.OLLAMA_KEEP_ALIVE
OLLAMA_NUM_CTX           = cfg.OLLAMA_NUM_CTX
_OLLAMA_NUM_CTX_OVERRIDES = {
    model.strip(): int(num_ctx.strip())
    for entry in cfg.OLLAMA_NUM_CTX_OVERRIDES.split(",")
    if entry.strip()
    for model, num_ctx in [entry.split("=", 1)]
}


def num_ctx_for(model: str) -> int:
    """Liefert die konfigurierte Kontextlänge für einen exakten Modell-Tag."""
    return _OLLAMA_NUM_CTX_OVERRIDES.get(model, OLLAMA_NUM_CTX)


def ollama_options(model: str, **opts) -> dict:
    """Ergänzt Ollama-Optionen um die für dieses Modell konfigurierte Kontextlänge."""
    return {**opts, "num_ctx": num_ctx_for(model)}


AGENT_MODEL_FAST         = cfg.AGENT_MODEL_FAST
AGENT_MODEL_STRONG       = cfg.AGENT_MODEL_STRONG
ADDRESS_CHECK_MODEL      = cfg.ADDRESS_CHECK_MODEL
VOICE_AGENT_MODEL        = cfg.VOICE_AGENT_MODEL
VOICE_AGENT_KEEP_ALIVE   = cfg.VOICE_AGENT_KEEP_ALIVE
ANTHROPIC_API_KEY        = cfg.ANTHROPIC_API_KEY
CLAUDE_CHAT_MODEL        = cfg.CLAUDE_CHAT_MODEL

BG_DEFAULT_MODEL         = cfg.BG_DEFAULT_MODEL or cfg.OLLAMA_MODEL
BG_REASONING_MODEL       = cfg.BG_REASONING_MODEL
BG_CODE_MODEL            = cfg.BG_CODE_MODEL
BG_CODE_TEMPERATURE      = cfg.BG_CODE_TEMPERATURE
VISION_MODEL             = cfg.VISION_MODEL

TELEGRAM_BOT_TOKEN       = cfg.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID         = cfg.TELEGRAM_CHAT_ID
TELEGRAM_ALLOWED_IDS     = cfg.TELEGRAM_ALLOWED_IDS

TRIGGER_TOKEN            = cfg.TRIGGER_TOKEN
LLM_LOCAL_ONLY           = cfg.LLM_LOCAL_ONLY
JEV_ENABLED              = cfg.JEV_ENABLED
JEV_FORGE_GATE_ENABLED   = cfg.JEV_FORGE_GATE_ENABLED
JEV_PROVIDER             = cfg.JEV_PROVIDER
TYPESAFE_API_KEY         = cfg.TYPESAFE_API_KEY
OPENROUTER_API_KEY       = cfg.OPENROUTER_API_KEY
JEV_MODEL                = cfg.JEV_MODEL
JEV_TIMEOUT_S            = cfg.JEV_TIMEOUT_S
JEV_COOLDOWN_S           = cfg.JEV_COOLDOWN_S
JEV_LOG_PATH             = cfg.JEV_LOG_PATH

OWNER_NAME               = cfg.OWNER_NAME
OWNER_EMAIL              = cfg.OWNER_EMAIL
OWNER_TIMEZONE           = cfg.OWNER_TIMEZONE

CALENDAR_ICS_URLS        = cfg.CALENDAR_ICS_URLS
COROS_MCP_URL            = cfg.COROS_MCP_URL

DATABASE_URL             = cfg.DATABASE_URL

VAPID_PRIVATE_KEY_PATH   = cfg.VAPID_PRIVATE_KEY_PATH
VAPID_PUBLIC_KEY         = cfg.VAPID_PUBLIC_KEY
VAPID_CLAIM_EMAIL        = cfg.VAPID_CLAIM_EMAIL_RESOLVED

BRAVE_API_KEY            = cfg.BRAVE_API_KEY

GOOGLE_CLIENT_ID         = cfg.GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET     = cfg.GOOGLE_CLIENT_SECRET
GOOGLE_CALENDAR_ID       = cfg.GOOGLE_CALENDAR_ID

THERMAL_TARGET_CELSIUS   = cfg.THERMAL_TARGET_CELSIUS
THERMAL_MAX_CELSIUS      = cfg.THERMAL_MAX_CELSIUS

IDLE_MIN_SLEEP_S         = cfg.IDLE_MIN_SLEEP_S
IDLE_MAX_SLEEP_S         = cfg.IDLE_MAX_SLEEP_S
IDLE_EVAL_AFTER_S        = cfg.IDLE_EVAL_AFTER_S

PROACTIVE_WAIT_AFTER_CONV = cfg.PROACTIVE_WAIT_AFTER_CONV
PROACTIVE_INTERVAL        = cfg.PROACTIVE_INTERVAL

ACCOUNTABILITY_ENABLED       = cfg.ACCOUNTABILITY_ENABLED
ACCOUNTABILITY_ANCHOR_TIME   = cfg.ACCOUNTABILITY_ANCHOR_TIME
ACCOUNTABILITY_BLOCK_START   = cfg.ACCOUNTABILITY_BLOCK_START
ACCOUNTABILITY_BLOCK_MINUTES = cfg.ACCOUNTABILITY_BLOCK_MINUTES

LZG_EMBED_MODEL          = cfg.LZG_EMBED_MODEL
LZG_TOP_K                = cfg.LZG_TOP_K
KZG_MAX_TURNS            = cfg.KZG_MAX_TURNS
