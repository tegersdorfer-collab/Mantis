import asyncio

from settings import MantisSettings


def test_voice_filler_settings_have_requested_defaults(monkeypatch):
    for name in (
        "VOICE_FILLERS_ACK", "VOICE_FILLERS_TOOL", "VOICE_FILLERS_ENABLED",
        "VOICE_FILLER_ACK_DELAY_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = MantisSettings(_env_file=None)

    assert settings.VOICE_FILLERS_ACK == ["Mhm.", "Okay.", "Moment."]
    assert settings.VOICE_FILLERS_TOOL == [
        "Ich schau kurz nach.",
        "Moment, ich check das.",
        "Sekunde, ich schau nach.",
    ]
    assert settings.VOICE_FILLERS_ENABLED is True
    assert settings.VOICE_FILLER_ACK_DELAY_MS == 800


def test_voice_filler_pools_can_be_overridden_as_json_environment_settings(monkeypatch):
    monkeypatch.setenv("VOICE_FILLERS_ACK", '["Äh.","Kurz."]')
    monkeypatch.setenv("VOICE_FILLERS_TOOL", '["Ich prüfe das."]')

    settings = MantisSettings(_env_file=None)

    assert settings.VOICE_FILLERS_ACK == ["Äh.", "Kurz."]
    assert settings.VOICE_FILLERS_TOOL == ["Ich prüfe das."]


def test_filler_cache_key_changes_with_engine_voice_or_text():
    from core.voice_fillers import cache_path

    base = cache_path("Mhm.", engine="cartesia", voice="nicos-voice")

    assert base.suffix == ".ogg"
    assert base != cache_path("Mhm.", engine="piper", voice="nicos-voice")
    assert base != cache_path("Mhm.", engine="cartesia", voice="other-voice")
    assert base != cache_path("Okay.", engine="cartesia", voice="nicos-voice")


def test_default_cache_identity_tracks_configured_tts_engine_and_voice(monkeypatch):
    import config
    from core import tts
    from core.voice_fillers import cache_path

    monkeypatch.setattr(tts, "_engine", lambda: "cartesia")
    monkeypatch.setattr(config, "TTS_CARTESIA_VOICE", "voice-a")
    cartesia_a = cache_path("Mhm.")
    monkeypatch.setattr(config, "TTS_CARTESIA_VOICE", "voice-b")
    cartesia_b = cache_path("Mhm.")
    monkeypatch.setattr(tts, "_engine", lambda: "kokoro")
    monkeypatch.setattr(config, "TTS_KOKORO_VOICE", "voice-b")
    kokoro_b = cache_path("Mhm.")

    assert cartesia_a != cartesia_b
    assert cartesia_b != kokoro_b


def test_preload_uses_fake_tts_once_and_reuses_cached_files(tmp_path, monkeypatch):
    import config
    import core.voice_fillers as fillers

    monkeypatch.setattr(fillers, "FILLERS_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE_FILLERS_ACK", ["Mhm.", "Okay."])
    monkeypatch.setattr(config, "VOICE_FILLERS_TOOL", ["Ich schau kurz nach."])
    calls = []

    async def fake_synthesize(text):
        calls.append(text)
        return f"OGG:{text}".encode()

    monkeypatch.setattr(fillers.tts, "synthesize", fake_synthesize)

    asyncio.run(fillers.preload_voice_fillers())
    asyncio.run(fillers.preload_voice_fillers())

    assert calls == ["Mhm.", "Okay.", "Ich schau kurz nach."]
    assert len(list(tmp_path.glob("*.ogg"))) == 3
    selected = fillers.choose_cached_filler("ack")
    assert selected is not None
    assert selected.kind == "ack"
    assert selected.audio.startswith(b"OGG:")


def test_preload_errors_are_swallowed_and_leave_no_cache_file(tmp_path, monkeypatch):
    import config
    import core.voice_fillers as fillers

    monkeypatch.setattr(fillers, "FILLERS_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE_FILLERS_ACK", ["Mhm."])
    monkeypatch.setattr(config, "VOICE_FILLERS_TOOL", [])

    async def broken_synthesize(_text):
        raise RuntimeError("fake TTS failure")

    monkeypatch.setattr(fillers.tts, "synthesize", broken_synthesize)

    asyncio.run(fillers.preload_voice_fillers())

    assert list(tmp_path.iterdir()) == []
    assert fillers.choose_cached_filler("ack") is None


def test_missing_runtime_cache_skips_filler_without_synthesizing(tmp_path, monkeypatch):
    import config
    import core.voice_fillers as fillers

    monkeypatch.setattr(fillers, "FILLERS_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE_FILLERS_ACK", ["Mhm."])
    calls = []

    async def fake_synthesize(text):
        calls.append(text)
        return b"should not run"

    monkeypatch.setattr(fillers.tts, "synthesize", fake_synthesize)

    assert fillers.choose_cached_filler("ack") is None
    assert calls == []


def test_consecutive_filler_selection_never_repeats_the_same_text(tmp_path, monkeypatch):
    import config
    import core.voice_fillers as fillers

    monkeypatch.setattr(fillers, "FILLERS_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE_FILLERS_ACK", ["Mhm."])
    monkeypatch.setattr(config, "VOICE_FILLERS_TOOL", ["Mhm."])
    monkeypatch.setattr(fillers, "_last_used_text", None)
    path = fillers.cache_path("Mhm.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OGG:Mhm.")

    assert fillers.choose_cached_filler("ack").text == "Mhm."
    assert fillers.choose_cached_filler("tool") is None


def test_scheduled_preload_starts_after_parakeet_finishes(monkeypatch):
    import config
    import core.voice_fillers as fillers

    monkeypatch.setattr(config, "VOICE_FILLERS_ENABLED", True)
    started = []

    async def fake_preload():
        started.append("fillers")

    monkeypatch.setattr(fillers, "preload_voice_fillers", fake_preload)

    async def run():
        parakeet_release = asyncio.Event()
        parakeet = asyncio.create_task(parakeet_release.wait())
        filler_task = fillers.schedule_voice_fillers_preload(parakeet)
        await asyncio.sleep(0)
        assert started == []
        parakeet_release.set()
        await filler_task

    asyncio.run(run())

    assert started == ["fillers"]


def test_opus_duration_is_read_from_cached_ogg_page():
    from core.voice_fillers import _ogg_opus_duration_ms

    pre_skip = 312
    duration_ms = 730
    granule = pre_skip + duration_ms * 48
    body = b"OpusHead" + bytes((1, 1)) + pre_skip.to_bytes(2, "little") + b"\0" * 7
    header = b"OggS\0\0" + granule.to_bytes(8, "little") + b"\0" * 12 + bytes((1,))
    ogg = header + bytes((len(body),)) + body

    assert _ogg_opus_duration_ms(ogg) == duration_ms

