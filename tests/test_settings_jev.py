"""Jev ist Opt-in: ohne JEV_ENABLED + Key bleibt alles lokal."""
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


def test_local_logits_sind_standardmaessig_aus_und_timeout_ist_kurz():
    s = MantisSettings(_env_file=None)
    assert s.LOCAL_LOGITS_ENABLED is False
    assert s.LOCAL_LOGITS_TIMEOUT_S == 8.0
