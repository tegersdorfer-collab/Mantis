"""Jev bleibt für Forge-Integrationen separat konfigurierbar."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settings import MantisSettings


def test_jev_defaults_sind_aus():
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is False
    assert s.JEV_FORGE_GATE_ENABLED is False
    assert s.JEV_PROVIDER == "typesafe"
    assert s.JEV_PROVIDER_CHAIN == ""
    assert s.TYPESAFE_API_KEY == ""
    assert s.OPENROUTER_API_KEY == ""
    assert s.JEV_MODEL == "jev-latest"
    assert s.CLEF_MODEL == "clef-flash"
    assert s.CLOUDFLARE_ACCOUNT_ID == ""
    assert s.CLOUDFLARE_AUTH_TOKEN == ""
    assert s.JEV_TIMEOUT_S == 3.0
    assert s.JEV_COOLDOWN_S == 120.0


def test_jev_aus_env(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_PROVIDER", "typesafe")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is True and s.TYPESAFE_API_KEY == "sk-test"


def test_provider_chain_und_cloudflare_settings_aus_env(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_CHAIN", "cloudflare,openrouter")
    monkeypatch.setenv("CLEF_MODEL", "clef")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-test")
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "token-test")

    s = MantisSettings(_env_file=None)

    assert s.JEV_PROVIDER_CHAIN == "cloudflare,openrouter"
    assert s.CLEF_MODEL == "clef"
    assert s.CLOUDFLARE_ACCOUNT_ID == "account-test"
    assert s.CLOUDFLARE_AUTH_TOKEN == "token-test"


def test_voice_silence_setting_defaults_to_800ms_and_requires_positive_value():
    from pydantic import ValidationError
    import pytest

    s = MantisSettings(_env_file=None)
    assert s.VOICE_SILENCE_MS == 800
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, VOICE_SILENCE_MS=0)


def test_adaptive_voice_settings_have_expected_defaults_and_require_positive_intervals():
    from pydantic import ValidationError
    import pytest

    s = MantisSettings(_env_file=None)

    assert s.VOICE_ADAPTIVE_SILENCE is True
    assert s.VOICE_SILENCE_FAST_MS == 450
    assert s.VOICE_SILENCE_SLOW_MS == 1200
    assert s.VOICE_PARTIAL_STT_MS == 700
    assert s.VOICE_PARTIAL_MAX_MS == 900
    assert s.VOICE_PARTIAL_STALE_MS == 400
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, VOICE_PARTIAL_STT_MS=0)
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, VOICE_PARTIAL_STALE_MS=0)
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, VOICE_PARTIAL_MAX_MS=0)
