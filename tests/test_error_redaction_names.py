import config
from core.error_redaction import redact_error_text


def test_redacts_bot_tokens(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:telegram-secret", raising=False)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "discord-secret-token", raising=False)
    text = redact_error_text("GET https://api.telegram.org/bot123456:telegram-secret/getUpdates discord-secret-token")
    assert "telegram-secret" not in text
    assert "discord-secret-token" not in text
