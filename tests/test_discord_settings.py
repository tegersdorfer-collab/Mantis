import pytest

from settings import MantisSettings


def test_discord_settings_default_to_disabled_and_strip_secret_values(monkeypatch):
    for name in (
        "DISCORD_ENABLED",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_CHANNEL_ID",
        "NOTIFY_CHANNEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = MantisSettings(
        _env_file=None,
        DISCORD_BOT_TOKEN="  bot-token\n",
        DISCORD_ALLOWED_USER_IDS=" 123, 456 ,, ",
        DISCORD_CHANNEL_ID=" 789 ",
    )

    assert settings.DISCORD_ENABLED is False
    assert settings.DISCORD_BOT_TOKEN == "bot-token"
    assert settings.DISCORD_ALLOWED_USER_IDS == ("123", "456")
    assert settings.DISCORD_CHANNEL_ID == "789"
    assert settings.NOTIFY_CHANNEL == "telegram"


def test_notify_channel_accepts_only_supported_channels():
    settings = MantisSettings(_env_file=None, NOTIFY_CHANNEL=" Discord ")

    assert settings.NOTIFY_CHANNEL == "discord"

    with pytest.raises(ValueError, match="NOTIFY_CHANNEL"):
        MantisSettings(_env_file=None, NOTIFY_CHANNEL="web")


def test_discord_allowlist_keeps_order_for_first_user_notifications():
    settings = MantisSettings(
        _env_file=None,
        DISCORD_ALLOWED_USER_IDS="456, 123",
    )

    assert settings.DISCORD_ALLOWED_USER_IDS == ("456", "123")
