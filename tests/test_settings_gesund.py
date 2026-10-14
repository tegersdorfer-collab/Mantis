from settings import MantisSettings


def test_chat_ttl_defaults_to_365_days_and_can_be_set_from_environment(monkeypatch):
    monkeypatch.delenv("CHAT_TTL_DAYS", raising=False)
    assert MantisSettings(_env_file=None).CHAT_TTL_DAYS == 365

    monkeypatch.setenv("CHAT_TTL_DAYS", "30")
    assert MantisSettings(_env_file=None).CHAT_TTL_DAYS == 30


def test_background_jobs_default_to_off_and_accountability_stays_off(monkeypatch):
    for name in (
        "INSIGHTS_ENABLED",
        "PATTERN_DETECTION_ENABLED",
        "TRAINING_PLAN_AUTOGEN_ENABLED",
        "NEWS_REFRESH_ENABLED",
        "DAILY_REFLECTION_ENABLED",
        "MEMORY_CONSOLIDATION_ENABLED",
        "TASK_DEEP_EXECUTION_ENABLED",
        "BACKGROUND_REVIEW_ENABLED",
        "ACCOUNTABILITY_ENABLED",
        "AUTONOMOUS_MESSAGES_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = MantisSettings(_env_file=None)

    assert settings.INSIGHTS_ENABLED is False
    assert settings.PATTERN_DETECTION_ENABLED is False
    assert settings.TRAINING_PLAN_AUTOGEN_ENABLED is False
    assert settings.NEWS_REFRESH_ENABLED is False
    assert settings.DAILY_REFLECTION_ENABLED is False
    assert settings.MEMORY_CONSOLIDATION_ENABLED is False
    assert settings.TASK_DEEP_EXECUTION_ENABLED is False
    assert settings.BACKGROUND_REVIEW_ENABLED is False
    assert settings.ACCOUNTABILITY_ENABLED is False
    assert settings.AUTONOMOUS_MESSAGES_ENABLED is False


def test_background_flags_can_be_enabled_from_environment(monkeypatch):
    monkeypatch.setenv("INSIGHTS_ENABLED", "true")

    assert MantisSettings(_env_file=None).INSIGHTS_ENABLED is True


def test_cartesia_tts_settings_default_to_selected_engine_and_voice(monkeypatch):
    for name in (
        "TTS_ENGINE",
        "CARTESIA_API_KEY",
        "TTS_CARTESIA_MODEL",
        "TTS_CARTESIA_VOICE",
        "TTS_CARTESIA_LANGUAGE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = MantisSettings(_env_file=None)

    assert settings.TTS_ENGINE == "cartesia"
    assert settings.CARTESIA_API_KEY == ""
    assert settings.TTS_CARTESIA_MODEL == "sonic-3.6"
    assert settings.TTS_CARTESIA_VOICE == "afa425cf-5489-4a09-8a3f-d3cb1f82150d"
    assert settings.TTS_CARTESIA_LANGUAGE == "de"
