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
    assert MantisSettings(_env_file=None).TTS_ENGINE == "cartesia"
