import config
from core.error_redaction import redact_error_text
import pytest


def test_redacts_bot_tokens(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:telegram-secret", raising=False)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "discord-secret-token", raising=False)
    text = redact_error_text("GET https://api.telegram.org/bot123456:telegram-secret/getUpdates discord-secret-token")
    assert "telegram-secret" not in text
    assert "discord-secret-token" not in text


def test_redacts_cartesia_key_from_error_text(monkeypatch):
    synthetic_key = "sk_car-synthetic-test-key"
    monkeypatch.setattr(config, "CARTESIA_API_KEY", synthetic_key, raising=False)

    text = redact_error_text(f"Request failed with {synthetic_key} (Bearer {synthetic_key})")

    assert synthetic_key not in text
    assert text.count("[REDACTED]") == 2


def test_redacts_google_tts_key_from_error_text(monkeypatch):
    synthetic_key = "google-tts-synthetic-test-key"
    monkeypatch.setattr(config, "GOOGLE_TTS_API_KEY", synthetic_key, raising=False)

    text = redact_error_text(f"Request failed with {synthetic_key}")

    assert synthetic_key not in text
    assert text.endswith("[REDACTED]")


@pytest.mark.db
def test_db_log_error_redacts_synthetic_api_key_in_summary_and_detail(monkeypatch):
    from core import db

    synthetic_key = "sk-or-synthetic-db-log-key"
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", synthetic_key, raising=False)
    events = []
    monkeypatch.setattr(db, "log_event", lambda *args: events.append(args))

    db.log_error("test", RuntimeError(f"request failed with {synthetic_key}"))

    assert len(events) == 1
    assert synthetic_key not in events[0][1]
    assert events[0][2]["error"] == "request failed with [REDACTED]"
