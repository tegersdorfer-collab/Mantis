from settings import MantisSettings


def test_situation_settings_default_to_enabled_and_five_minutes(monkeypatch):
    monkeypatch.delenv("SITUATION_ENABLED", raising=False)
    monkeypatch.delenv("SITUATION_REFRESH_S", raising=False)

    settings = MantisSettings(_env_file=None)

    assert settings.SITUATION_ENABLED is True
    assert settings.SITUATION_REFRESH_S == 300
