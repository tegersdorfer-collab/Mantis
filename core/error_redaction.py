"""Zentrale Redaktion sensibler Werte aus Fehlertexten vor Log und Persistenz."""
import os
import re

_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)([^\s,;\"']+)")
_REDACTED = "[REDACTED]"
_SECRET_NAMES = (
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN", "DASHBOARD_TOKEN", "TYPESAFE_API_KEY", "CLOUDFLARE_AUTH_TOKEN",
)


def redact_error_text(value) -> str:
    """Entfernt bekannte API-Keys und Bearer-Tokens aus einem Fehlertext."""
    try:
        text = str(value)
    except Exception:
        text = "<Fehlertext nicht lesbar>"

    secrets = {
        os.environ.get(name, "").strip()
        for name in _SECRET_NAMES
    }
    try:
        import config
        secrets.update(
            str(getattr(config, name, "") or "").strip()
            for name in _SECRET_NAMES
        )
    except Exception:
        pass

    for secret in sorted((s for s in secrets if len(s) >= 4), key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return _BEARER_TOKEN.sub(rf"\1{_REDACTED}", text)
