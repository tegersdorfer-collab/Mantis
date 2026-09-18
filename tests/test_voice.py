"""Unit-Tests für core/voice.py: whisper.cpp-Transkription + Adress-Check."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Diese Tests patchen pywhispercpp.model.Model — das Paket muss dafür importierbar
# sein. Fehlt es (z.B. schlanke CI ohne schwere ML-Wheels), wird die Datei geskippt.
pytest.importorskip("pywhispercpp")

import core.voice as voice


@pytest.fixture(autouse=True)
def _reset_conversation_window():
    """Verhindert Test-Verschmutzung über den globalen 'Konversation aktiv'-Status hinweg,
    z.B. wenn test_voice_router.py vorher lief und mark_conversation_active() real aufgerufen hat."""
    voice._conversation_active_until = 0.0
    yield
    voice._conversation_active_until = 0.0


class TestTranscribeAudio:
    def setup_method(self):
        voice._whisper_model = None  # sauberer Start pro Test

    def test_gibt_transkribierten_text_zurueck(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="  Wie war mein Schlaf?  ")]
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "Wie war mein Schlaf?"

    def test_verkettet_mehrere_segmente(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="Hallo"), MagicMock(text="Mantis")]
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == "Hallo Mantis"

    def test_laedt_modell_nur_einmal(self):
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [MagicMock(text="test")]
        with patch("pywhispercpp.model.Model", return_value=fake_model) as mock_load:
            asyncio.run(voice.transcribe_audio("/tmp/a.wav"))
            asyncio.run(voice.transcribe_audio("/tmp/b.wav"))
        mock_load.assert_called_once()

    def test_transkribiert_nicht_parallel_bei_gleichzeitigen_aufrufen(self):
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

    def test_transkriptions_fehler_gibt_leeren_string(self):
        fake_model = MagicMock()
        fake_model.transcribe.side_effect = RuntimeError("kaputt")
        with patch("pywhispercpp.model.Model", return_value=fake_model):
            text = asyncio.run(voice.transcribe_audio("/tmp/fake.wav"))
        assert text == ""

    def test_fehlendes_whisper_paket_gibt_leeren_string(self):
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
        with patch("core.fast.yes_no", new=AsyncMock()) as mock_yes_no:
            result = asyncio.run(voice.is_addressed_to_mantis("Mantis, wie wird das Wetter morgen?"))
        assert result is True
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
        assert mock_yes_no.call_args.kwargs["model"] == voice.config.ADDRESS_CHECK_MODEL

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


def test_address_check_nutzt_jev_vor_fast():
    """Layer 3: Jev entscheidet; fast.yes_no ist nur noch der Fallback."""
    from unittest.mock import AsyncMock, patch
    from core import voice
    voice._conversation_active_until = 0.0
    with patch("core.voice.decisions.addressed", new=AsyncMock(return_value=True)) as jev, \
         patch("core.voice.fast.yes_no", new=AsyncMock(return_value=False)) as fast:
        assert asyncio.run(voice.is_addressed_to_mantis("mach das Licht an")) is True
    jev.assert_awaited_once()
    fast.assert_not_awaited()
    text, fallback = jev.await_args.args
    assert text == "mach das Licht an" and callable(fallback)
