"""Jev ist Opt-in: ohne JEV_ENABLED + Key bleibt alles lokal."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settings import MantisSettings


def test_jev_defaults_sind_aus():
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is False
    assert s.OPENROUTER_API_KEY == ""
    assert s.JEV_MODEL == "~typesafe/jev-latest"
    assert s.JEV_TIMEOUT_S == 3.0
    assert s.JEV_COOLDOWN_S == 120.0


def test_jev_aus_env(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    s = MantisSettings(_env_file=None)
    assert s.JEV_ENABLED is True and s.OPENROUTER_API_KEY == "sk-test"
