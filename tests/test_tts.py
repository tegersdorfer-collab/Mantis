"""Unit-Tests für core/tts.py: Kokoro (Default) + Piper (Fallback), Text → OGG/Opus."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import MagicMock

import pytest

import core.tts as tts


@pytest.fixture(autouse=True)
def _reset_engine_state(monkeypatch):
    """Jeder Test startet ohne geladene Engine. Default ist seit 22.09.2026 Kokoro;
    die Piper-Tests zwingen die Engine per monkeypatch auf 'piper'."""
    monkeypatch.setattr(tts, "_kokoro", None)
    monkeypatch.setattr(tts, "_kokoro_unavailable", False)
    monkeypatch.setattr(tts, "_voice", None)
    yield


@pytest.fixture
def piper_engine(monkeypatch):
    monkeypatch.setattr(tts.config, "TTS_ENGINE", "piper")
    yield


@pytest.fixture
def kokoro_bereit(tmp_path, monkeypatch):
    """Tut so, als lägen die Kokoro-Modelldateien vor."""
    (tmp_path / "kokoro-v1.0.onnx").write_bytes(b"x")
    (tmp_path / "voices-v1.0.bin").write_bytes(b"x")
    monkeypatch.setattr(tts, "_KOKORO_DIR", tmp_path)
    monkeypatch.setattr(tts.config, "TTS_ENGINE", "kokoro")
    yield tmp_path


class TestKokoro:
    """Kokoro ist die Default-Engine (0,52 s bis zum ersten Ton gegen 1,06 s bei Piper)."""

    def test_verfuegbar_wenn_modelldateien_da_sind(self, kokoro_bereit):
        assert tts.is_available() is True

    def test_nutzt_kokoro_statt_piper(self, kokoro_bereit, monkeypatch):
        monkeypatch.setattr(tts, "_load_kokoro", lambda: MagicMock())
        monkeypatch.setattr(tts, "_synth_kokoro", lambda text, speed: b"KOKORO_WAV")
        monkeypatch.setattr(tts, "_synth", lambda text, speed: pytest.fail("Piper darf nicht laufen"))
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)
        assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:KOKORO_WAV"

    def test_faellt_auf_piper_zurueck_wenn_kokoro_nicht_laedt(self, kokoro_bereit, monkeypatch):
        """Fehlt espeak-ng oder kokoro-onnx, muss Mantis weiter sprechen können."""
        monkeypatch.setattr(tts, "_load_kokoro", lambda: None)
        monkeypatch.setattr(tts, "_synth", lambda text, speed: b"PIPER_WAV")
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)
        assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:PIPER_WAV"

    def test_fehlendes_modell_schaltet_dauerhaft_auf_piper(self, tmp_path, monkeypatch):
        """Ohne Modelldateien darf nicht bei jeder Antwort erneut geladen werden."""
        monkeypatch.setattr(tts, "_KOKORO_DIR", tmp_path)   # leer
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "kokoro")
        assert tts._load_kokoro() is None
        assert tts._kokoro_unavailable is True

    def test_is_available_bleibt_true_wenn_nur_piper_da_ist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tts, "_KOKORO_DIR", tmp_path)   # kein Kokoro
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "kokoro")
        onnx = tmp_path / "de_DE-thorsten-high.onnx"; onnx.write_bytes(b"x")
        (tmp_path / "de_DE-thorsten-high.onnx.json").write_bytes(b"{}")
        monkeypatch.setattr(tts, "_MODEL_DIR", tmp_path)
        monkeypatch.setattr(tts.db, "get_setting", lambda key, default=None: "thorsten-high")
        assert tts.is_available() is True

    def test_meldet_die_kokoro_stimme(self, kokoro_bereit, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_KOKORO_VOICE", "dm_martin")
        assert asyncio.run(tts.list_voices()) == ["dm_martin"]


class TestIsAvailable:
    def test_verfuegbar_wenn_modell_dateien_existieren(self, tmp_path, monkeypatch, piper_engine):
        onnx = tmp_path / "de_DE-thorsten-high.onnx"
        cfg = tmp_path / "de_DE-thorsten-high.onnx.json"
        onnx.write_bytes(b"x")
        cfg.write_bytes(b"{}")
        monkeypatch.setattr(tts, "_MODEL_DIR", tmp_path)
        monkeypatch.setattr(tts.db, "get_setting", lambda key, default=None: "thorsten-high")
        assert tts.is_available() is True

    def test_nicht_verfuegbar_wenn_dateien_fehlen(self, tmp_path, monkeypatch, piper_engine):
        monkeypatch.setattr(tts, "_MODEL_DIR", tmp_path)
        monkeypatch.setattr(tts.db, "get_setting", lambda key, default=None: "thorsten-high")
        assert tts.is_available() is False

    def test_nicht_verfuegbar_bei_unbekanntem_stimmen_namen(self, monkeypatch, piper_engine):
        monkeypatch.setattr(tts.db, "get_setting", lambda key, default=None: "unbekannt")
        assert tts.is_available() is False


class TestResolveVoicePaths:
    def test_loest_bekannte_stimme_auf(self):
        onnx, cfg = tts.resolve_voice_paths("thorsten-high")
        assert onnx.name == "de_DE-thorsten-high.onnx"
        assert cfg.name == "de_DE-thorsten-high.onnx.json"

    def test_loest_alle_vier_kandidaten_auf(self):
        for name in ["thorsten-high", "thorsten_emotional-medium", "karlsson-low", "pavoque-low"]:
            onnx, cfg = tts.resolve_voice_paths(name)
            assert onnx.suffix == ".onnx"
            assert cfg.name == onnx.name + ".json"

    def test_wirft_bei_unbekannter_stimme(self):
        import pytest
        with pytest.raises(KeyError):
            tts.resolve_voice_paths("nicht-existent")


class TestCleanForSpeech:
    def test_entfernt_markdown(self):
        assert tts._clean_for_speech("**fett** und *kursiv* und `code`") == "fett und kursiv und code"

    def test_entfernt_links(self):
        assert tts._clean_for_speech("Schau [hier](https://example.com) nach") == "Schau hier nach"


class TestSynthesize:
    def setup_method(self):
        tts._voice = None  # sauberer Start pro Test

    def test_gibt_leere_bytes_wenn_nicht_verfuegbar(self, monkeypatch):
        monkeypatch.setattr(tts, "is_available", lambda: False)
        result = asyncio.run(tts.synthesize("Hallo"))
        assert result == b""

    def test_gibt_leere_bytes_bei_leerem_text(self, monkeypatch):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        result = asyncio.run(tts.synthesize("   "))
        assert result == b""

    def test_synthetisiert_und_konvertiert_zu_ogg(self, monkeypatch, piper_engine):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        monkeypatch.setattr(tts, "_synth", lambda text, speed: b"FAKE_WAV")
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"FAKE_OGG")
        result = asyncio.run(tts.synthesize("Hallo Timo"))
        assert result == b"FAKE_OGG"

    def test_fehler_gibt_leere_bytes_zurueck(self, monkeypatch, piper_engine):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        def boom(text, speed):
            raise RuntimeError("kaputt")
        monkeypatch.setattr(tts, "_synth", boom)
        result = asyncio.run(tts.synthesize("Hallo"))
        assert result == b""

    def test_kuerzt_zu_langen_text(self, monkeypatch, piper_engine):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        captured = {}
        def fake_synth(text, speed):
            captured["text"] = text
            return b"WAV"
        monkeypatch.setattr(tts, "_synth", fake_synth)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG")
        long_text = ("Ein Satz. " * 300) + "Ende."
        asyncio.run(tts.synthesize(long_text, max_chars=50))
        assert len(captured["text"]) <= 50


class TestStreamingSplit:
    """Die Blockbildung entscheidet, ob die Wiedergabe durchläuft.

    Während Block i+1 berechnet wird, läuft Block i als Ton. Reißt das Verhältnis,
    hört man eine Lücke mitten in der Antwort — schlimmer als ein späterer Start.
    """

    def test_schneidet_nur_an_satzgrenzen(self):
        bloecke = tts._split_for_streaming(
            "Erster Satz hier. Zweiter Satz da. Dritter Satz dort.")
        for b in bloecke:
            assert b.endswith((".", "!", "?", "…")), f"Block endet mitten im Satz: {b!r}"

    def test_kurzer_einstiegssatz_wird_aufgefuellt(self):
        """'Ja.' allein puffert ~0,3 s — zu wenig für die Berechnung des nächsten Blocks."""
        bloecke = tts._split_for_streaming(
            "Ja. " + "Morgen um vierzehn Uhr hast du den Zahnarzttermin, und es soll "
            "etwa achtzehn Grad warm werden, mit Regen am Nachmittag und Wind aus Nordwest.")
        assert len(bloecke[0]) >= tts._STREAM_MIN_CHARS

    def test_bloecke_wachsen_nur_begrenzt(self):
        """Kein Block darf viel länger sein als sein Vorgänger — sonst überholt die
        Wiedergabe die Synthese."""
        text = " ".join(f"Das ist Satz Nummer {i} mit ein paar zusätzlichen Wörtern." for i in range(1, 12))
        bloecke = tts._split_for_streaming(text)
        for vorher, nachher in zip(bloecke, bloecke[1:]):
            assert len(nachher) <= tts._STREAM_WACHSTUM * len(vorher) + len(vorher), \
                f"Sprung von {len(vorher)} auf {len(nachher)} Zeichen"

    def test_kurzer_text_bleibt_ein_block(self):
        assert tts._split_for_streaming("Alles klar, die Lampe ist aus.") == \
               ["Alles klar, die Lampe ist aus."]

    def test_leerer_text_ergibt_keine_bloecke(self):
        assert tts._split_for_streaming("   ") == []

    def test_verliert_keinen_text(self):
        text = "Erster Satz. Zweiter Satz! Dritter Satz? Vierter Satz."
        assert " ".join(tts._split_for_streaming(text)).split() == text.split()


class TestSynthesizeStream:
    def test_liefert_einen_block_pro_abschnitt(self, kokoro_bereit, monkeypatch):
        monkeypatch.setattr(tts, "_load_kokoro", lambda: MagicMock())
        monkeypatch.setattr(tts, "_synth_kokoro", lambda text, speed: b"WAV:" + text.encode())
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)
        text = " ".join(f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern." for i in range(1, 8))

        async def sammeln():
            return [c async for c in tts.synthesize_stream(text)]
        bloecke = asyncio.run(sammeln())
        assert len(bloecke) == len(tts._split_for_streaming(tts._clean_for_speech(text)))
        assert all(b.startswith(b"OGG:WAV:") for b in bloecke)

    def test_defekter_block_stoppt_den_rest_nicht(self, kokoro_bereit, monkeypatch):
        """Ein Fehler im zweiten Block darf die restliche Antwort nicht verschlucken."""
        monkeypatch.setattr(tts, "_load_kokoro", lambda: MagicMock())
        aufrufe = {"n": 0}

        def flaky(text, speed):
            aufrufe["n"] += 1
            if aufrufe["n"] == 2:
                raise RuntimeError("kaputt")
            return b"WAV"
        monkeypatch.setattr(tts, "_synth_kokoro", flaky)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG")
        text = " ".join(f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern." for i in range(1, 8))

        async def sammeln():
            return [c async for c in tts.synthesize_stream(text)]
        bloecke = asyncio.run(sammeln())
        assert aufrufe["n"] >= 3                      # nach dem Fehler weitergelaufen
        assert len(bloecke) == aufrufe["n"] - 1       # nur der defekte fehlt

    def test_nichts_wenn_tts_nicht_verfuegbar(self, monkeypatch):
        monkeypatch.setattr(tts, "is_available", lambda: False)

        async def sammeln():
            return [c async for c in tts.synthesize_stream("Hallo")]
        assert asyncio.run(sammeln()) == []

    def test_faellt_auf_piper_zurueck(self, kokoro_bereit, monkeypatch):
        monkeypatch.setattr(tts, "_load_kokoro", lambda: None)
        monkeypatch.setattr(tts, "_synth", lambda text, speed: b"PIPER")
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)

        async def sammeln():
            return [c async for c in tts.synthesize_stream("Alles klar, die Lampe ist aus.")]
        assert asyncio.run(sammeln()) == [b"OGG:PIPER"]
