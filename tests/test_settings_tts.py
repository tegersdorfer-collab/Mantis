from settings import MantisSettings


def test_google_tts_secret_voice_and_engine_defaults():
    settings = MantisSettings(
        _env_file=None,
        GOOGLE_TTS_API_KEY="google-tts-synthetic-secret",
        TTS_ENGINE="google",
    )

    assert settings.GOOGLE_TTS_API_KEY.get_secret_value() == "google-tts-synthetic-secret"
    assert "google-tts-synthetic-secret" not in repr(settings)
    assert settings.TTS_GOOGLE_VOICE == "de-DE-Chirp3-HD-Charon"
    assert settings.TTS_ENGINE_CHAIN == ["cartesia", "klon", "elevenlabs", "google", "local"]
    assert MantisSettings(_env_file=None).TTS_ENGINE == "cartesia"


def test_elevenlabs_settings_keep_api_key_secret_and_default_voice_and_price():
    settings = MantisSettings(
        _env_file=None,
        ELEVENLABS_API_KEY="elevenlabs-synthetic-secret",
    )

    assert settings.ELEVENLABS_API_KEY.get_secret_value() == "elevenlabs-synthetic-secret"
    assert "elevenlabs-synthetic-secret" not in repr(settings)
    assert settings.TTS_ELEVENLABS_MODEL == "eleven_v4_turbo"
    assert settings.TTS_ELEVENLABS_VOICE == "iP95p4xoKVk53GoZ742B"
    assert settings.TTS_ELEVENLABS_USD_PER_MILLION_CHARS == 0.0
    assert settings.TTS_QUOTA_COOLDOWN_S == 3600.0


def test_tts_engine_chain_can_be_configured_from_environment(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE_CHAIN", '["google","local"]')

    settings = MantisSettings(_env_file=None)

    assert settings.TTS_ENGINE_CHAIN == ["google", "local"]


def test_klon_settings_include_voice_clone_defaults_and_merge_overrides():
    settings = MantisSettings(
        _env_file=None,
        TTS_KLON_PRONUNCIATIONS={"Branch": "Brantsch", "Feature": "Fietscher"},
    )

    assert settings.TTS_KLON_IDLE_S == 600.0
    assert settings.TTS_KLON_PYTHON.endswith("/.venv-tts/bin/python")
    assert settings.TTS_KLON_REF_DIR.endswith("/data/voice_clone")
    assert settings.TTS_KLON_PRONUNCIATIONS["Branch"] == "Brantsch"
    assert settings.TTS_KLON_PRONUNCIATIONS["Deployment"] == "Diploiment"
    assert settings.TTS_KLON_PRONUNCIATIONS["Feature"] == "Fietscher"


def test_klon_worker_timeouts_have_defaults_and_environment_overrides(monkeypatch):
    defaults = MantisSettings(_env_file=None)
    assert defaults.TTS_KLON_START_TIMEOUT_S == 20.0
    assert defaults.TTS_KLON_FIRST_CHUNK_TIMEOUT_S == 3.0
    assert defaults.TTS_KLON_CHUNK_TIMEOUT_S == 3.0

    monkeypatch.setenv("TTS_KLON_START_TIMEOUT_S", "25")
    monkeypatch.setenv("TTS_KLON_FIRST_CHUNK_TIMEOUT_S", "4")
    monkeypatch.setenv("TTS_KLON_CHUNK_TIMEOUT_S", "5")
    configured = MantisSettings(_env_file=None)
    assert configured.TTS_KLON_START_TIMEOUT_S == 25.0
    assert configured.TTS_KLON_FIRST_CHUNK_TIMEOUT_S == 4.0
    assert configured.TTS_KLON_CHUNK_TIMEOUT_S == 5.0


def test_klon_pronunciations_can_be_extended_with_json_environment(monkeypatch):
    monkeypatch.setenv("TTS_KLON_PRONUNCIATIONS", '{"Playlist":"Pleylist"}')

    settings = MantisSettings(_env_file=None)

    assert settings.TTS_KLON_PRONUNCIATIONS["Playlist"] == "Pleylist"
    assert settings.TTS_KLON_PRONUNCIATIONS["Spotify"] == "Spottifei"
