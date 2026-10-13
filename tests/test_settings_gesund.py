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
