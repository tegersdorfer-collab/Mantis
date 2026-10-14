"""Unit-Tests für core/voice.py: Parakeet-/whisper.cpp-Transkription + Adress-Check."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

import core.voice as voice


@pytest.fixture(autouse=True)
def _reset_conversation_window():
    """Verhindert Test-Verschmutzung über den globalen 'Konversation aktiv'-Status hinweg,
    z.B. wenn test_voice_router.py vorher lief und mark_conversation_active() real aufgerufen hat.

    Setzt außerdem den STT-Lock zurück: sobald ein Test echte Konkurrenz erzeugt, bindet
    sich asyncio.Lock an dessen Event-Loop, und der nächste asyncio.run() würde daran
    scheitern. In Mantis läuft alles in EINEM Loop — das ist reine Test-Hygiene."""
    voice._conversation_active_until = 0.0
    voice._whisper_lock = asyncio.Lock()
    yield
    voice._conversation_active_until = 0.0


@pytest.fixture
def whisper_engine(monkeypatch):
    """Zwingt den whisper.cpp-Pfad. Seit 22.09.2026 ist Parakeet der Default; die
    whisper-Tests decken weiterhin den Fallback ab, der bei fehlendem parakeet-mlx greift."""
    pytest.importorskip("pywhispercpp")
    monkeypatch.setattr(voice.config, "STT_ENGINE", "whisper")
    yield


class TestTranscribeParakeet:
    """Parakeet ist seit 22.09.2026 die Default-Engine (Benchmark: Weckname 72 % vs 28 %)."""

    @pytest.fixture(autouse=True)
    def _model_not_in_external_cache(self, monkeypatch):
        import huggingface_hub

        def cache_miss(_repo_id, _filename, *, local_files_only=False, **_kwargs):
            assert local_files_only is True
            raise FileNotFoundError("Testmodell nicht im lokalen HF-Cache")

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", cache_miss)

    def setup_method(self):
        voice._parakeet_model = None
        voice._parakeet_unavailable = False
        voice._whisper_model = None

    def teardown_method(self):
        voice._parakeet_model = None
        voice._parakeet_unavailable = False

    def _fake_parakeet(self, text="Okay Mantis"):
        model = MagicMock()
        model.transcribe.return_value = MagicMock(text=f"  {text}  ")
        return model

    def test_nutzt_parakeet_wenn_verfuegbar(self, monkeypatch):
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        fake = self._fake_parakeet()
        mod = MagicMock(from_pretrained=MagicMock(return_value=fake))
        with patch.dict("sys.modules", {"parakeet_mlx": mod}):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "Okay Mantis"
        mod.from_pretrained.assert_called_once_with(voice.config.STT_PARAKEET_MODEL)

    @pytest.mark.parametrize(("raw", "expected"), [
        ("Mantasy sehen …", "Mantis sehen …"),
        ("Bantas, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mantes, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mantiss, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mentis, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mandis, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mäntis, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mantus, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mandes, erzähl mir was.", "Mantis, erzähl mir was."),
        ("mantir, erzähl mir was.", "Mantis, erzähl mir was."),
        ("Hey, mantes, mach weiter.", "Hey, Mantis, mach weiter."),
        ("mantis, erzähl mir was.", "Mantis, erzähl mir was."),
        ("Mangos kaufen", "Mangos kaufen"),
        ("Mangas kaufen", "Mangas kaufen"),
        ("Martin kaufen", "Martin kaufen"),
        ("Nantes kaufen", "Nantes kaufen"),
        ("Mantel anziehen", "Mantel anziehen"),
        ("Mandeln kaufen", "Mandeln kaufen"),
        ("Manchmal regnet es", "Manchmal regnet es"),
        ("Mantra kaufen", "Mantra kaufen"),
        ("Mandarin kaufen", "Mandarin kaufen"),
        ("Bantu kaufen", "Bantu kaufen"),
        ("Bandas, erzähl mir was.", "Bandas, erzähl mir was."),
        ("Bitte Mantasy erklären", "Bitte Mantasy erklären"),
    ])
    def test_normalisiert_nur_mantis_aehnliches_erstes_wort(self, monkeypatch, raw, expected):
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        model = MagicMock()
        model.transcribe.return_value = MagicMock(text=raw)
        voice._parakeet_model = model

        text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))

        assert text == expected

    def test_lader_nutzt_hf_cache_zuerst_ohne_netzwerk(self, monkeypatch, tmp_path):
        import huggingface_hub

        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        (snapshot / "config.json").write_text("{}")
        (snapshot / "model.safetensors").write_bytes(b"weights")
        calls = []

        def cached_download(repo_id, filename, *, local_files_only=False, **_kwargs):
            calls.append((repo_id, filename, local_files_only))
            assert local_files_only is True
            return str(snapshot / filename)

        expected_model = object()
        package = MagicMock(from_pretrained=MagicMock(return_value=expected_model))
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", cached_download)
        monkeypatch.setitem(sys.modules, "parakeet_mlx", package)

        loaded = voice._load_parakeet_model("mlx-community/parakeet-tdt-0.6b-v3")

        assert loaded is expected_model
        assert calls == [
            ("mlx-community/parakeet-tdt-0.6b-v3", "config.json", True),
            ("mlx-community/parakeet-tdt-0.6b-v3", "model.safetensors", True),
        ]
        package.from_pretrained.assert_called_once_with(str(snapshot))

    def test_lader_downloadet_nur_wenn_hf_cache_fehlgeschlagen_ist(self, monkeypatch):
        import huggingface_hub

        def cache_miss(_repo_id, _filename, *, local_files_only=False, **_kwargs):
            assert local_files_only is True
            raise FileNotFoundError("nicht im Cache")

        package = MagicMock(from_pretrained=MagicMock(return_value="model"))
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", cache_miss)
        monkeypatch.setitem(sys.modules, "parakeet_mlx", package)

        assert voice._load_parakeet_model("mlx-community/parakeet-tdt-0.6b-v3") == "model"

        package.from_pretrained.assert_called_once_with("mlx-community/parakeet-tdt-0.6b-v3")

    def test_vorladen_startet_im_hintergrund_und_fehler_brechen_nicht_durch(self, monkeypatch):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        monkeypatch.setattr(voice.config, "STT_PRELOAD", True, raising=False)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-parakeet")
        monkeypatch.setattr(voice, "_parakeet_executor", executor)
        caller_thread = threading.get_ident()
        loader_threads = []

        def fail_load(_model_id):
            loader_threads.append(threading.get_ident())
            raise RuntimeError("Modell fehlt")

        monkeypatch.setattr(voice, "_load_parakeet_model", fail_load)

        async def run_preload():
            task = voice.schedule_parakeet_preload()
            assert task is not None
            await task

        asyncio.run(run_preload())
        executor.shutdown(wait=True)

        assert loader_threads and loader_threads[0] != caller_thread
        assert voice._parakeet_unavailable is False

    def test_vorladen_ist_per_setting_abschaltbar(self, monkeypatch):
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        monkeypatch.setattr(voice.config, "STT_PRELOAD", False, raising=False)
        monkeypatch.setattr(voice, "_load_parakeet_model", lambda _model_id: pytest.fail("soll nicht laden"))

        assert voice.schedule_parakeet_preload() is None

    def test_vorladen_laedt_nicht_wenn_whisper_ausgewaehlt_ist(self, monkeypatch):
        monkeypatch.setattr(voice.config, "STT_ENGINE", "whisper")
        monkeypatch.setattr(voice.config, "STT_PRELOAD", True, raising=False)

        assert voice.schedule_parakeet_preload() is None


    def test_laedt_modell_nur_einmal(self, monkeypatch):
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        mod = MagicMock(from_pretrained=MagicMock(return_value=self._fake_parakeet()))
        with patch.dict("sys.modules", {"parakeet_mlx": mod}):
            asyncio.run(voice.transcribe_audio("/tmp/a.wav"))
            asyncio.run(voice.transcribe_audio("/tmp/b.wav"))
        mod.from_pretrained.assert_called_once()

    def test_laden_und_transkribieren_laufen_auf_demselben_thread(self, monkeypatch):
        """MLX bindet seinen GPU-Stream an den Thread, der das Modell geladen hat."""
        from concurrent.futures import ThreadPoolExecutor
        from functools import partial
        import itertools
        import threading

        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        load_threads = []

        class ThreadBoundModel:
            def transcribe(self, _path):
                if threading.get_ident() != load_threads[0]:
                    raise RuntimeError("There is no Stream(gpu, 0) in current thread")
                return MagicMock(text="Okay Mantis")

        def load(_model_id):
            load_threads.append(threading.get_ident())
            return ThreadBoundModel()

        mod = MagicMock(from_pretrained=load)
        workers = [ThreadPoolExecutor(max_workers=1) for _ in range(2)]
        next_worker = itertools.count()

        async def alternating_to_thread(function, *args, **kwargs):
            loop = asyncio.get_running_loop()
            worker = workers[next(next_worker) % len(workers)]
            return await loop.run_in_executor(worker, partial(function, *args, **kwargs))

        monkeypatch.setattr(voice.asyncio, "to_thread", alternating_to_thread)
        try:
            with patch.dict("sys.modules", {"parakeet_mlx": mod}):
                assert asyncio.run(voice.transcribe_audio("/tmp/fake.wav")) == "Okay Mantis"
        finally:
            for worker in workers:
                worker.shutdown(wait=True)

        assert len(load_threads) == 1
        assert voice._parakeet_unavailable is False

    def test_faellt_auf_whisper_zurueck_wenn_paket_fehlt(self, monkeypatch):
        """Ohne parakeet-mlx muss Mantis weiter Sprache verstehen — über whisper.cpp."""
        pytest.importorskip("pywhispercpp")
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "parakeet_mlx":
                raise ImportError("nicht installiert")
            return real_import(name, *args, **kwargs)

        fake_whisper = MagicMock()
        fake_whisper.transcribe.return_value = [MagicMock(text="von whisper")]
        with patch("builtins.__import__", side_effect=fake_import), \
             patch("pywhispercpp.model.Model", return_value=fake_whisper):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "von whisper"
        assert voice._parakeet_unavailable is True

    def test_ladefehler_schaltet_dauerhaft_auf_whisper(self, monkeypatch):
        """Ein fehlgeschlagener Modell-Download (z.B. kein Netz) darf nicht bei JEDER
        Äußerung erneut versucht werden."""
        pytest.importorskip("pywhispercpp")
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        mod = MagicMock(from_pretrained=MagicMock(side_effect=RuntimeError("kein Netz")))
        fake_whisper = MagicMock()
        fake_whisper.transcribe.return_value = [MagicMock(text="von whisper")]
        with patch.dict("sys.modules", {"parakeet_mlx": mod}), \
             patch("pywhispercpp.model.Model", return_value=fake_whisper):
            assert asyncio.run(voice.transcribe_audio("/tmp/a.wav")) == "von whisper"
            assert asyncio.run(voice.transcribe_audio("/tmp/b.wav")) == "von whisper"
        mod.from_pretrained.assert_called_once()  # kein zweiter Ladeversuch

    def test_transkriptions_fehler_wechselt_die_engine_nicht(self, monkeypatch):
        """Eine kaputte Audiodatei ist kein Grund, dauerhaft auf das schlechtere
        Modell umzuschalten — nur dieser eine Aufruf liefert leer."""
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        fake = MagicMock()
        fake.transcribe.side_effect = RuntimeError("kaputte Datei")
        mod = MagicMock(from_pretrained=MagicMock(return_value=fake))
        with patch.dict("sys.modules", {"parakeet_mlx": mod}):
            assert asyncio.run(voice.transcribe_audio("/tmp/fake.wav")) == ""
        assert voice._parakeet_unavailable is False

    def test_transkribiert_nicht_parallel(self, monkeypatch):
        """Ein STT-Modell auf einmal — sonst stehen auf dem 16-GB-Mac zwei Modelle
        gleichzeitig im Speicher (siehe RAM-Crash 18.09.2026)."""
        monkeypatch.setattr(voice.config, "STT_ENGINE", "parakeet")
        import threading, time
        max_concurrent = current = 0
        lock = threading.Lock()

        def fake_transcribe(path):
            nonlocal max_concurrent, current
            with lock:
                current += 1
                max_concurrent = max(max_concurrent, current)
            time.sleep(0.05)
            with lock:
                current -= 1
            return MagicMock(text=path)

        fake = MagicMock()
        fake.transcribe.side_effect = fake_transcribe
        mod = MagicMock(from_pretrained=MagicMock(return_value=fake))
        with patch.dict("sys.modules", {"parakeet_mlx": mod}):
            async def run_both():
                await asyncio.gather(
                    voice.transcribe_audio("/tmp/a.wav"),
                    voice.transcribe_audio("/tmp/b.wav"),
                )
            asyncio.run(run_both())
        assert max_concurrent == 1


def test_voice_settings_default_to_short_answers_and_stt_preload(monkeypatch):
    monkeypatch.delenv("VOICE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("STT_PRELOAD", raising=False)
    from settings import MantisSettings

    settings = MantisSettings(_env_file=None)

    assert settings.VOICE_MAX_TOKENS == 500
    assert settings.STT_PRELOAD is True


def test_whisper_voice_transcript_also_normalizes_the_initial_wake_word(monkeypatch):
    monkeypatch.setattr(voice.config, "STT_ENGINE", "whisper")
    monkeypatch.setattr(
        voice, "_transcribe_whisper", AsyncMock(return_value="Bantas, zeig mir das."),
    )

    assert asyncio.run(voice.transcribe_audio("/tmp/fake.wav")) == "Mantis, zeig mir das."


class TestTranscribeAudio:
    def setup_method(self):
        voice._whisper_model = None  # sauberer Start pro Test

    def test_gibt_transkribierten_text_zurueck(self, whisper_engine):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="  Wie war mein Schlaf?  ")]
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "Wie war mein Schlaf?"

    def test_verkettet_mehrere_segmente(self, whisper_engine):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="Hallo"), MagicMock(text="Mantis")]
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "Hallo Mantis"

    def test_laedt_modell_nur_einmal(self, whisper_engine):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="test")]
        with patch("pywhispercpp.model.Model", return_value=fake_model) as mock_load:
            asyncio.run(voice.transcribe_audio("/tmp/a.wav"))
            asyncio.run(voice.transcribe_audio("/tmp/b.wav"))
        mock_load.assert_called_once()

    def test_transkribiert_nicht_parallel_bei_gleichzeitigen_aufrufen(self, whisper_engine):
        """whisper.cpp/ggml crasht (SIGABRT) bei parallelen transcribe()-Aufrufen auf
        demselben Modell-Kontext — beide Aufrufe müssen serialisiert laufen, auch wenn
        transcribe_audio() gleichzeitig für zwei Segmente aufgerufen wird."""
        import threading
        import time

        max_concurrent = 0
        current = 0
        lock = threading.Lock()

        def fake_transcribe(path):
            nonlocal max_concurrent, current
            with lock:
                current += 1
                max_concurrent = max(max_concurrent, current)
            time.sleep(0.05)
            with lock:
                current -= 1
            return [MagicMock(text=path)]

        fake_model = MagicMock()
        fake_model.transcribe.side_effect = fake_transcribe
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            async def run_both():
                await asyncio.gather(
                    voice.transcribe_audio("/tmp/a.wav"),
                    voice.transcribe_audio("/tmp/b.wav"),
                )
            asyncio.run(run_both())

        assert max_concurrent == 1

    def test_transkriptions_fehler_gibt_leeren_string(self, whisper_engine):
        fake_model = MagicMock()
        fake_model.transcribe.side_effect = RuntimeError("kaputt")
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == ""

    def test_fehlendes_whisper_paket_gibt_leeren_string(self, whisper_engine):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pywhispercpp.model":
                raise ImportError("nicht installiert")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == ""


class TestIsAddressedToMantis:
    def test_namensnennung_liefert_true_ohne_llm_call(self):
        with patch("core.decisions.voice_address", new=AsyncMock()) as mock_jev, \
             patch("core.fast.yes_no", new=AsyncMock()) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("Mantis, wie wird das Wetter morgen?"))
        assert result is True
        mock_jev.assert_not_awaited()
        mock_yes_no.assert_not_called()

    def test_namensnennung_gross_klein_unabhaengig(self):
        with patch("core.fast.yes_no", new=AsyncMock()) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("hey MANTIS wie geht's"))
        assert result is True
        mock_yes_no.assert_not_called()

    def test_ohne_namen_faellt_auf_kleines_modell_zurueck_ja(self):
        with patch("core.fast.yes_no", new=AsyncMock(return_value=True)) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("Ruf mir die Nacht-Zusammenfassung auf"))
        assert result is True
        mock_yes_no.assert_called_once()
        assert mock_yes_no.call_args.kwargs == {}

    def test_jev_entscheidet_vor_dem_cloud_fallback(self):
        jev = AsyncMock(return_value=True)
        with patch("core.decisions.voice_address", new=jev), \
             patch("core.fast.yes_no", new=AsyncMock()) as fast:
            result = asyncio.run(voice.is_addressed_to_mantis("Ruf mir die Nacht-Zusammenfassung auf"))
        assert result is True
        jev.assert_awaited_once_with("Ruf mir die Nacht-Zusammenfassung auf")
        fast.assert_not_awaited()

    def test_jev_unsicherheit_faellt_auf_die_bestehende_cloud_kette_zurueck(self):
        jev = AsyncMock(return_value=None)
        fast = AsyncMock(return_value=False)
        with patch("core.decisions.voice_address", new=jev), \
             patch("core.fast.yes_no", new=fast):
            result = asyncio.run(voice.is_addressed_to_mantis("Ich rede mit jemand anderem"))
        assert result is False
        jev.assert_awaited_once()
        fast.assert_awaited_once()

    def test_jev_und_cloud_fallback_teilen_sich_eine_deadline(self, monkeypatch):
        monkeypatch.setattr(voice, "VOICE_ADDRESS_TIMEOUT_S", 0.04, raising=False)
        calls = []

        async def slow_jev(_text):
            calls.append("jev")
            await asyncio.sleep(0.03)
            return None

        async def slow_cloud(_question):
            calls.append("cloud")
            await asyncio.sleep(0.03)
            return True

        monkeypatch.setattr("core.decisions.voice_address", slow_jev)
        monkeypatch.setattr("core.fast.yes_no", slow_cloud)

        result = asyncio.run(voice.is_addressed_to_mantis("Bitte hilf mir"))

        assert result is False
        assert calls == ["jev", "cloud"]

    def test_ohne_namen_faellt_auf_kleines_modell_zurueck_nein(self):
        with patch("core.fast.yes_no", new=AsyncMock(return_value=False)):
            result = asyncio.run(voice.is_addressed_to_mantis("Ich rede gerade mit jemand anderem"))
        assert result is False

    def test_leerer_text_liefert_false_ohne_llm_call(self):
        with patch("core.fast.yes_no", new=AsyncMock()) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis(""))
        assert result is False
        mock_yes_no.assert_not_called()


class TestConversationFollowup:
    """Nach einer Mantis-Antwort gilt ein kurzes Zeitfenster, in dem Folge-Sprache
    automatisch als adressiert gilt — sonst ignoriert Mantis kurze Antworten wie
    'ja', 'zeig mir das' oder 'und morgen?', die keinen Namen/klaren Befehl enthalten."""

    def setup_method(self):
        voice._conversation_active_until = 0.0

    def test_folge_antwort_innerhalb_des_fensters_gilt_als_adressiert(self):
        voice.mark_conversation_active()
        with patch("core.fast.yes_no", new=AsyncMock()) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("ja genau das meinte ich"))
        assert result is True
        mock_yes_no.assert_not_called()

    def test_ausserhalb_des_fensters_normaler_check(self):
        voice._conversation_active_until = 0.0  # Fenster abgelaufen/nie aktiv
        with patch("core.fast.yes_no", new=AsyncMock(return_value=False)) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("ja genau das meinte ich"))
        assert result is False
        mock_yes_no.assert_called_once()

    def test_leerer_text_bleibt_false_auch_im_fenster(self):
        voice.mark_conversation_active()
        result = asyncio.run(voice.is_addressed_to_mantis(""))
        assert result is False


def test_address_check_uses_fast_cloud_variant_directly():
    from unittest.mock import AsyncMock, patch
    from core import voice
    voice._conversation_active_until = 0.0
    with patch("core.decisions.addressed", new=AsyncMock(return_value=True)) as jev, \
         patch("core.voice.fast.yes_no", new=AsyncMock(return_value=False)) as fast:
        assert asyncio.run(voice.is_addressed_to_mantis("mach das Licht an")) is False
    jev.assert_not_awaited()
    fast.assert_awaited_once_with(
        "Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten "
        "namens Mantis (nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name "
        "'Mantis' nicht genannt wird?\n\n\"mach das Licht an\""
    )


def test_address_check_timeout_during_jev_fails_closed(monkeypatch):
    monkeypatch.setattr(voice, "VOICE_ADDRESS_TIMEOUT_S", 0.01, raising=False)

    async def slow_jev(_text):
        await asyncio.sleep(0.05)

    fast = AsyncMock(return_value=True)
    monkeypatch.setattr("core.decisions.voice_address", slow_jev)
    monkeypatch.setattr("core.voice.fast.yes_no", fast)

    assert asyncio.run(voice.is_addressed_to_mantis("mach das Licht an")) is False
    fast.assert_not_awaited()
