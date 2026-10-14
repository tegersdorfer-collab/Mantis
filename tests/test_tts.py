"""Unit-Tests für core/tts.py: Kokoro (Default) + Piper (Fallback), Text → OGG/Opus."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import logging
from unittest.mock import MagicMock

import httpx
import pytest

import core.tts as tts


@pytest.fixture(autouse=True)
def _reset_engine_state(monkeypatch):
    """Jeder Test startet ohne geladene Engine. Default ist seit 22.09.2026 Kokoro;
    die Piper-Tests zwingen die Engine per monkeypatch auf 'piper'."""
    monkeypatch.setattr(tts, "_kokoro", None)
    monkeypatch.setattr(tts, "_kokoro_unavailable", False)
    monkeypatch.setattr(tts, "_voice", None)
    monkeypatch.setattr(tts, "_cartesia_paused_until", 0.0, raising=False)
    monkeypatch.setattr(tts, "_cartesia_pause_warned", False, raising=False)
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


def _mock_cartesia(monkeypatch, handler):
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    timeouts = []

    def make_client(**kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(
        tts.httpx,
        "AsyncClient",
        make_client,
    )
    return timeouts


@pytest.fixture
def cartesia_engine(monkeypatch):
    monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
    monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "sk_car_test_key", raising=False)
    monkeypatch.setattr(tts.config, "TTS_CARTESIA_MODEL", "sonic-3.6", raising=False)
    monkeypatch.setattr(
        tts.config,
        "TTS_CARTESIA_VOICE",
        "afa425cf-5489-4a09-8a3f-d3cb1f82150d",
        raising=False,
    )
    monkeypatch.setattr(tts.config, "TTS_CARTESIA_LANGUAGE", "de", raising=False)
    # Test-Fakes sind keine echten WAVs: nur echte RIFF-Daten werden wirklich geparst.
    real_nframes = tts._wav_nframes
    monkeypatch.setattr(
        tts, "_wav_nframes", lambda wav: real_nframes(wav) if wav.startswith(b"RIFF") else 1
    )


def _fake_local_synthesis(monkeypatch):
    monkeypatch.setattr(tts, "_load_kokoro", lambda: MagicMock())
    monkeypatch.setattr(
        tts,
        "_synth_kokoro",
        lambda text, speed: b"KOKORO_WAV:" + text.encode(),
    )
    monkeypatch.setattr(tts, "_synth", lambda text, speed: pytest.fail("Piper unerwartet"))
    monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)


class TestCartesia:
    def test_schreibt_live_zahlenbeispiele_vor_cartesia_aus(
        self, cartesia_engine, monkeypatch
    ):
        requests = []
        _mock_cartesia(
            monkeypatch,
            lambda request: (requests.append(request) or httpx.Response(200, content=b"WAV")),
        )
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: b"OGG")
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)

        result = asyncio.run(tts.synthesize(
            "vom 12. auf den 13. Oktober, 10,4 Stunden. "
            "Die Nacht davor waren 8 Stunden. 8:30 Uhr und 53 bpm."
        ))

        assert result == b"OGG"
        assert json.loads(requests[0].content)["transcript"] == (
            "vom zwölften auf den dreizehnten Oktober, zehn Komma vier Stunden. "
            "Die Nacht davor waren acht Stunden. acht Uhr dreißig und dreiundfünfzig bpm."
        )

    def test_sendet_cartesia_felder_und_gibt_ogg_zurueck(
        self, cartesia_engine, monkeypatch
    ):
        monkeypatch.setattr(tts, "_load_kokoro", lambda: pytest.fail("Kokoro darf nicht laden"))
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OggS" + wav)
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b"CARTESIA_WAV")

        timeouts = _mock_cartesia(monkeypatch, handler)
        usage = []
        from core import llm_usage
        monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: usage.append((args, kwargs)))

        result = asyncio.run(tts.synthesize("**Hallo** [Timo](https://example.com)!"))

        assert result == b"OggSCARTESIA_WAV"
        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://api.cartesia.ai/tts/bytes"
        assert request.headers["Authorization"] == "Bearer sk_car_test_key"
        assert request.headers["Cartesia-Version"] == "2025-04-16"
        assert timeouts == [5.0]
        assert json.loads(request.content) == {
            "model_id": "sonic-3.6",
            "transcript": "Hallo Timo!",
            "voice": {"mode": "id", "id": "afa425cf-5489-4a09-8a3f-d3cb1f82150d"},
            "language": "de",
            "output_format": {
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 44100,
            },
        }
        assert len(usage) == 1
        args, kwargs = usage[0]
        assert args[:4] == ("cartesia", "sonic-3.6", len("Hallo Timo!"), 0)
        assert kwargs["purpose"] == "tts"
        assert kwargs["actual_cost"] == pytest.approx(len("Hallo Timo!") * 65 / 1_000_000)
        assert tts._kokoro is None

    def test_cartesia_key_macht_tts_ohne_lokale_modelle_verfuegbar(
        self, cartesia_engine, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(tts, "_MODEL_DIR", tmp_path)
        monkeypatch.setattr(tts, "_KOKORO_DIR", tmp_path)

        assert tts.is_available() is True

    def test_faellt_ohne_api_key_auf_kokoro_zurueck(self, cartesia_engine, monkeypatch):
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "")
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        monkeypatch.setattr(
            tts.httpx,
            "AsyncClient",
            lambda **kwargs: pytest.fail("Ohne Key darf kein HTTP-Aufruf erfolgen"),
        )

        assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:KOKORO_WAV:Hallo"

    @pytest.mark.parametrize(
        "failure",
        ["network", "timeout", "http_400", "http_401", "http_402", "http_403", "http_429", "http_500"],
    )
    def test_faellt_bei_cartesia_fehlern_auf_kokoro_zurueck(
        self, cartesia_engine, monkeypatch, failure
    ):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        requests = []

        def handler(request):
            requests.append(request)
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("zu langsam", request=request)
            status = int(failure.removeprefix("http_"))
            return httpx.Response(status, content=b"Cartesia-Fehler")

        _mock_cartesia(monkeypatch, handler)

        assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:KOKORO_WAV:Hallo"
        assert len(requests) == 1

    @pytest.mark.parametrize("status", [401, 402, 403, 429])
    def test_auth_und_kontingent_pausieren_cartesia_30_minuten_einmalig(
        self, cartesia_engine, monkeypatch, caplog, status
    ):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 1000.0)
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(status, content=b"Fehler")

        _mock_cartesia(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger=tts.__name__)

        for _ in range(2):
            assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:KOKORO_WAV:Hallo"

        assert len(requests) == 1
        assert tts._cartesia_paused_until == 1000.0 + 30 * 60
        warnings = [record for record in caplog.records if "Cartesia TTS pausiert" in record.message]
        assert len(warnings) == 1

    def test_stream_gibt_jeden_cartesia_block_sofort_in_reihenfolge(
        self, cartesia_engine, monkeypatch
    ):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        requested = []

        async def fake_cartesia(text):
            requested.append(text)
            return b"WAV:" + text.encode()

        monkeypatch.setattr(tts, "_cartesia_synthesize", fake_cartesia)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda text: None)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)
        text = " ".join(
            f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern."
            for i in range(1, 8)
        )
        expected = tts._split_for_streaming(tts._clean_for_speech(text))

        async def collect():
            stream = tts.synthesize_stream(text)
            first = await anext(stream)
            assert requested == expected[:1]
            rest = [chunk async for chunk in stream]
            return [first, *rest]

        chunks = asyncio.run(collect())

        assert requested == expected
        assert chunks == [b"OGG:WAV:" + block.encode() for block in expected]


class TestCartesiaKonvertierung:
    @staticmethod
    def _fake_converter(monkeypatch, threads=None):
        import subprocess
        import threading

        def convert(wav):
            if threads is not None:
                threads.append(threading.get_ident())
            if wav == b"":
                return b""
            if wav.startswith(b"GARBAGE"):
                raise subprocess.CalledProcessError(1, "ffmpeg")
            return b"OGG:" + wav

        monkeypatch.setattr(tts, "_wav_to_ogg", convert)

    @pytest.mark.parametrize("body", [b"", b"GARBAGE-bytes"])
    def test_kaputte_cartesia_audio_faellt_lokal_zurueck(
        self, cartesia_engine, monkeypatch, caplog, body
    ):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        self._fake_converter(monkeypatch)
        local = []
        monkeypatch.setattr(
            tts, "_synth_kokoro", lambda text, speed: local.append(text) or b"KOKORO_WAV:" + text.encode()
        )
        _mock_cartesia(monkeypatch, lambda request: httpx.Response(200, content=body))
        caplog.set_level(logging.WARNING, logger=tts.__name__)

        result = asyncio.run(tts.synthesize("Hallo"))

        assert result == b"OGG:KOKORO_WAV:Hallo"
        assert local == ["Hallo"]
        assert tts._cartesia_paused_until == 0.0
        assert any("Cartesia" in r.message for r in caplog.records)

    def test_stream_ersetzt_nur_kaputten_block_lokal_und_haelt_reihenfolge(
        self, cartesia_engine, monkeypatch
    ):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        self._fake_converter(monkeypatch)
        text = " ".join(
            f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern."
            for i in range(1, 8)
        )
        expected = tts._split_for_streaming(tts._clean_for_speech(text))
        assert len(expected) >= 3
        kaputt = expected[1]

        async def fake_cartesia(t):
            return b"GARBAGE" if t == kaputt else b"WAV:" + t.encode()

        monkeypatch.setattr(tts, "_cartesia_synthesize", fake_cartesia)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda t: None)
        local = []
        monkeypatch.setattr(
            tts, "_synth_kokoro", lambda t, speed: local.append(t) or b"KOKORO_WAV:" + t.encode()
        )

        async def collect():
            return [c async for c in tts.synthesize_stream(text)]

        chunks = asyncio.run(collect())

        assert local == [kaputt]
        assert chunks[1] == b"OGG:KOKORO_WAV:" + kaputt.encode()
        assert chunks[0] == b"OGG:WAV:" + expected[0].encode()
        assert len(chunks) == len(expected)

    def test_konvertierung_laeuft_nicht_im_event_loop_thread(
        self, cartesia_engine, monkeypatch
    ):
        import threading

        monkeypatch.setattr(tts, "is_available", lambda: True)
        threads = []
        self._fake_converter(monkeypatch, threads)
        _mock_cartesia(monkeypatch, lambda request: httpx.Response(200, content=b"WAV"))
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda t: None)
        loop_thread = []

        async def run():
            loop_thread.append(threading.get_ident())
            return await tts.synthesize("Hallo")

        assert asyncio.run(run()) == b"OGG:WAV"
        assert threads and threads[0] != loop_thread[0]

    def test_kosten_nutzen_einstellung_pro_million_zeichen(
        self, cartesia_engine, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_CARTESIA_USD_PER_MILLION_CHARS", 100.0, raising=False)
        usage = []
        from core import llm_usage
        monkeypatch.setattr(llm_usage, "record", lambda *a, **k: usage.append(k))

        tts._record_cartesia_usage("x" * 1000)

        assert usage[0]["actual_cost"] == pytest.approx(0.1)

    def test_kosten_default_ist_65_usd(self):
        import settings

        assert settings.MantisSettings.model_fields["TTS_CARTESIA_USD_PER_MILLION_CHARS"].default == 65.0


def _leeres_wav() -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
    data = buf.getvalue()
    assert len(data) == 44
    return data


class TestCartesiaLeeresWav:
    def test_synthesize_nutzt_lokal_bei_wav_ohne_samples(self, cartesia_engine, monkeypatch, caplog):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        # Konverter liefert (wie ffmpeg) trotzdem ein nicht-leeres OGG.
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + (wav if not wav.startswith(b"RIFF") else b"EMPTYOGG"))
        wav = _leeres_wav()
        _mock_cartesia(monkeypatch, lambda request: httpx.Response(200, content=wav))
        caplog.set_level(logging.WARNING, logger=tts.__name__)

        assert asyncio.run(tts.synthesize("Hallo")) == b"OGG:KOKORO_WAV:Hallo"
        assert tts._cartesia_paused_until == 0.0
        assert any("Cartesia" in r.message for r in caplog.records)

    def test_stream_nutzt_lokal_bei_wav_ohne_samples(self, cartesia_engine, monkeypatch):
        monkeypatch.setattr(tts, "is_available", lambda: True)
        _fake_local_synthesis(monkeypatch)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + (wav if not wav.startswith(b"RIFF") else b"EMPTYOGG"))
        text = " ".join(
            f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern."
            for i in range(1, 8)
        )
        expected = tts._split_for_streaming(tts._clean_for_speech(text))
        leer = _leeres_wav()

        async def fake_cartesia(t):
            return leer if t == expected[1] else b"WAV:" + t.encode()

        monkeypatch.setattr(tts, "_cartesia_synthesize", fake_cartesia)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda t: None)

        async def collect():
            return [c async for c in tts.synthesize_stream(text)]

        chunks = asyncio.run(collect())

        assert len(chunks) == len(expected)
        assert chunks[1] == b"OGG:KOKORO_WAV:" + expected[1].encode()
        assert chunks[0] == b"OGG:WAV:" + expected[0].encode()


def test_cartesia_pronunciation_replaces_whole_words_only(monkeypatch):
    from core import tts
    monkeypatch.setattr(tts.config, "TTS_CARTESIA_PRONUNCIATIONS", {"Mantis": "Mäntis"})
    assert tts._apply_cartesia_pronunciations("Hallo, ich bin Mantis. mantis!") == "Hallo, ich bin Mäntis. Mäntis!"
    assert tts._apply_cartesia_pronunciations("Gottesanbeterinnen heißen Mantiden") == "Gottesanbeterinnen heißen Mantiden"


def test_cartesia_number_spelling_has_a_fallback_without_num2words(monkeypatch):
    monkeypatch.setattr(tts, "_num2words", None)

    assert tts._number_to_german_words(53) == "dreiundfünfzig"


def test_cartesia_numeric_date_uses_the_correct_ordinal_case():
    assert tts._numbers_to_german_words(
        "am 12.10. und 12. Oktober; außerdem 20%."
    ) == "am zwölften Oktober und zwölfter Oktober; außerdem zwanzig Prozent."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234 Schritte", "tausendzweihundertvierunddreißig Schritte"),
        ("2.000 kcal", "zweitausend kcal"),
        ("Dein Ruhepuls ist 53.", "Dein Ruhepuls ist dreiundfünfzig."),
        ("im 3. Satz", "im dritten Satz"),
        ("der 3. Satz", "der dritte Satz"),
        ("3. Satz", "Dritter Satz"),
        ("Version 3.14", "Version drei Punkt vierzehn"),
        ("Version 1.2.3", "Version eins Punkt zwei Punkt drei"),
        ("Version 1.13.5", "Version eins Punkt dreizehn Punkt fünf"),
        ("24:00 Uhr", "vierundzwanzig Uhr"),
        ("vom 12. auf den 13. Oktober", "vom zwölften auf den dreizehnten Oktober"),
        ("vom 12.–14.10.", "vom zwölften bis vierzehnten Oktober"),
        ("2026", "zweitausendsechsundzwanzig"),
        ("-3 Grad", "minus drei Grad"),
        ("1234567", "eins zwei drei vier fünf sechs sieben"),
        ("123 4567", "eins zwei drei, vier fünf sechs sieben"),
        (
            "0911 0123456",
            "null neun eins eins, null eins zwei drei vier fünf sechs",
        ),
        ("2,5 kg", "zwei Komma fünf Kilo"),
        ("2,5kg", "zwei Komma fünf Kilo"),
        ("Wert 3.14", "Wert 3.14"),
    ],
)
def test_cartesia_number_spelling_handles_review_regressions(text, expected):
    assert tts._numbers_to_german_words(text) == expected


def test_numbers_versions_and_two_digit_years():
    from core import tts
    assert tts._numbers_to_german_words("Version 3.14 und 1.2.3") == "Version drei Punkt vierzehn und eins Punkt zwei Punkt drei"
    assert tts._numbers_to_german_words("Update auf v2.4.1 ist da") == "Update auf Version zwei Punkt vier Punkt eins ist da"
    assert tts._numbers_to_german_words("am 12.10.26") == "am zwölften Oktober zweitausendsechsundzwanzig"
    assert tts._numbers_to_german_words("am 12.10.2026") == "am zwölften Oktober zweitausendsechsundzwanzig"


def test_numbers_review_round_three():
    from core import tts
    from core.voice_text import last_complete_sentence
    assert tts._numbers_to_german_words("Version 1.2.3.") == "Version eins Punkt zwei Punkt drei."
    assert tts._numbers_to_german_words("0,5 kg") == "null Komma fünf Kilo"
    assert tts._numbers_to_german_words("10,04 Stunden") == "zehn Komma null vier Stunden"
    assert last_complete_sentence("Termin am 3.11.26. Danach war") == "Termin am 3.11.26."
    assert last_complete_sentence("Am 12. Oktober war") == "Am 12. Oktober war"
