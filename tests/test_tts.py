"""Unit-Tests für core/tts.py: Kokoro (Default) + Piper (Fallback), Text → OGG/Opus."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64
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
    monkeypatch.setattr(tts, "_engine_paused_until", {}, raising=False)
    monkeypatch.setattr(tts, "_engine_error_paused_until", {}, raising=False)
    monkeypatch.setattr(tts, "_engine_quota_paused_until", {}, raising=False)
    monkeypatch.setattr(tts, "_engine_pause_warned", set(), raising=False)
    monkeypatch.setattr(tts, "_google_tts_paused_until", 0.0, raising=False)
    monkeypatch.setattr(tts, "_elevenlabs_paused_until", 0.0, raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "elevenlabs", "google", "local"], raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", False, raising=False)
    monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "", raising=False)
    monkeypatch.setattr(tts.config, "ELEVENLABS_API_KEY", "", raising=False)
    monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "", raising=False)
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

    def test_erster_block_darf_an_frueher_kommagrenze_endigen(self):
        text = "Das ist eine ausreichend lange Einleitung, danach kommt der Rest."
        bloecke = tts._split_for_streaming(text)
        assert bloecke[0] == "Das ist eine ausreichend lange Einleitung,"
        assert " ".join(bloecke).split() == text.split()

    def test_dezimalkomma_ist_keine_fruehe_schnittgrenze(self):
        text = "Der Messwert beträgt ungefähr 10,4 Stunden, und danach ging es weiter."
        bloecke = tts._split_for_streaming(text)
        assert bloecke[0] == "Der Messwert beträgt ungefähr 10,4 Stunden,"
        assert "10,4" in bloecke[0]

    @pytest.mark.parametrize(("text", "zahl"), [
        ("Dein Termin ist heute um 18:30 Uhr, danach fahren wir nach Hause.", "18:30"),
        ("Deine Arbeitszeit läuft von 10–12 Uhr, danach ist Pause.", "10–12"),
    ])
    def test_fruehe_schnittgrenze_zerlegt_keine_uhrzeiten_oder_zahlenbereiche(self, text, zahl):
        bloecke = tts._split_for_streaming(text)
        assert any(zahl in block for block in bloecke)

    def test_fruehe_schnittgrenze_braucht_mindestens_25_zeichen(self):
        text = "Das ist kurz, dann geht es weiter."
        assert tts._split_for_streaming(text) == [text]

    def test_erster_block_darf_an_gedankenstrich_oder_doppelpunkt_endigen(self):
        gedankenstrich = "Der erste Gedanke ist lang genug – danach folgt der Rest."
        doppelpunkt = "Hier steht eine lange Einleitung: danach folgt der Rest."
        assert tts._split_for_streaming(gedankenstrich)[0].endswith("–")
        assert tts._split_for_streaming(doppelpunkt)[0].endswith(":")

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


@pytest.fixture
def google_engine(monkeypatch):
    monkeypatch.setattr(tts.config, "TTS_ENGINE", "google", raising=False)
    monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-tts-test-key", raising=False)
    monkeypatch.setattr(
        tts.config,
        "TTS_GOOGLE_VOICE",
        "de-DE-Chirp3-HD-Charon",
        raising=False,
    )
    monkeypatch.setattr(
        tts.config,
        "TTS_CARTESIA_PRONUNCIATIONS",
        {"Mantis": "Mäntis"},
        raising=False,
    )
    monkeypatch.setattr(tts.db, "get_setting", lambda *_args: tts.DEFAULT_VOICE)


def _mock_google(monkeypatch, handler):
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    timeouts = []

    def make_client(**kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(tts.httpx, "AsyncClient", make_client)
    return timeouts


def _fake_local_synthesis(monkeypatch):
    monkeypatch.setattr(tts, "_load_kokoro", lambda: MagicMock())
    monkeypatch.setattr(
        tts,
        "_synth_kokoro",
        lambda text, speed: b"KOKORO_WAV:" + text.encode(),
    )
    monkeypatch.setattr(tts, "_synth", lambda text, speed: pytest.fail("Piper unerwartet"))
    monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OGG:" + wav)


def _fake_ogg_opus(samples: int = 4800) -> bytes:
    pre_skip = 312
    body = b"OpusHead" + bytes((1, 1)) + pre_skip.to_bytes(2, "little") + b"\0" * 7
    granule = pre_skip + samples
    header = b"OggS\0\0" + granule.to_bytes(8, "little") + b"\0" * 12 + bytes((1,))
    return header + bytes((len(body),)) + body


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

        result = asyncio.run(tts.synthesize("**Mantis** hat 2 Termine."))

        assert result == b"OggSCARTESIA_WAV"
        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://api.cartesia.ai/tts/bytes"
        assert request.headers["Authorization"] == "Bearer sk_car_test_key"
        assert request.headers["Cartesia-Version"] == "2025-04-16"
        assert timeouts == [5.0]
        assert json.loads(request.content) == {
            "model_id": "sonic-3.6",
            "transcript": "Mäntis hat zwei Termine.",
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
        assert args[:4] == ("cartesia", "sonic-3.6", len("Mäntis hat zwei Termine."), 0)
        assert kwargs["purpose"] == "tts"
        assert kwargs["actual_cost"] == pytest.approx(len("Mäntis hat zwei Termine.") * 65 / 1_000_000)
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
    def test_cartesia_error_cooldown_uses_quota_or_short_duration(
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
        expected_cooldown = 3600.0 if status in {402, 429} else 60.0
        assert tts._cartesia_paused_until == 1000.0 + expected_cooldown
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
        assert tts._cartesia_is_paused()
        assert any("Cartesia" in r.message for r in caplog.records)

    def test_stream_bleibt_nach_provider_wechsel_bei_der_fallback_stimme(
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

        assert local == expected[1:]
        assert chunks == [
            b"OGG:WAV:" + expected[0].encode(),
            *[b"OGG:KOKORO_WAV:" + block.encode() for block in expected[1:]],
        ]
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


class TestGoogle:
    def test_google_ogg_ohne_opus_samples_nutzt_naechste_engine(self, google_engine, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "is_available", lambda: True)
        monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)
        fallback = []
        expected_audio = _fake_ogg_opus()

        async def empty_google(_text):
            return _fake_ogg_opus(0), False

        async def local(text, _speed):
            fallback.append(text)
            return b"LOCAL_WAV"

        monkeypatch.setattr(tts, "_google_tts_synthesize", empty_google)
        monkeypatch.setattr(tts, "_synthesize_local", local)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: expected_audio)

        assert asyncio.run(tts.synthesize("Hallo.")) == expected_audio
        assert fallback == ["Hallo."]

    def test_google_linear16_ohne_samples_nutzt_naechste_engine(self, google_engine, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "is_available", lambda: True)
        monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)
        fallback = []
        expected_audio = _fake_ogg_opus()

        async def empty_google(_text):
            return b"GOOGLE_LINEAR16_WAV", True

        async def local(text, _speed):
            fallback.append(text)
            return b"LOCAL_WAV"

        def convert(wav):
            return _fake_ogg_opus(0) if wav == b"GOOGLE_LINEAR16_WAV" else expected_audio

        monkeypatch.setattr(tts, "_google_tts_synthesize", empty_google)
        monkeypatch.setattr(tts, "_synthesize_local", local)
        monkeypatch.setattr(tts, "_wav_to_ogg", convert)

        assert asyncio.run(tts.synthesize("Hallo.")) == expected_audio
        assert fallback == ["Hallo."]

    def test_synthesizes_ogg_mit_header_stimme_normalisierung_und_kosten(
        self, google_engine, monkeypatch
    ):
        audio = _fake_ogg_opus()
        requests = []
        _mock_google(
            monkeypatch,
            lambda request: (
                requests.append(request)
                or httpx.Response(
                    200,
                    json={"audioContent": base64.b64encode(audio).decode("ascii")},
                )
            ),
        )
        usage = []
        from core import llm_usage
        monkeypatch.setattr(llm_usage, "record", lambda *a, **k: usage.append((a, k)))

        result = asyncio.run(tts.synthesize("**Mantis** hat 2 Termine."))

        assert result == audio
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://texttospeech.googleapis.com/v1/text:synthesize"
        assert not request.url.query
        assert request.headers["x-goog-api-key"] == "google-tts-test-key"
        payload = json.loads(request.content)
        assert payload == {
            "input": {"text": "Mäntis hat zwei Termine."},
            "voice": {"languageCode": "de-DE", "name": "de-DE-Chirp3-HD-Charon"},
            "audioConfig": {"audioEncoding": "OGG_OPUS", "sampleRateHertz": 24000},
        }
        assert len(usage) == 1
        args, kwargs = usage[0]
        assert args[:4] == ("google", "chirp3-hd", len("Mäntis hat zwei Termine."), 0)
        assert kwargs["purpose"] == "tts"
        assert kwargs["actual_cost"] == pytest.approx(len("Mäntis hat zwei Termine.") * 30 / 1_000_000)

    def test_wechselt_bei_abgelehntem_ogg_zu_linear16_und_konvertiert(
        self, google_engine, monkeypatch
    ):
        requests = []
        audio = _fake_ogg_opus()

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": "OGG_OPUS audio encoding is not supported",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={"audioContent": base64.b64encode(b"GOOGLE_WAV").decode("ascii")},
            )

        _mock_google(monkeypatch, handler)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: audio)

        result = asyncio.run(tts.synthesize("Hallo."))

        assert result == audio
        assert [json.loads(request.content)["audioConfig"] for request in requests] == [
            {"audioEncoding": "OGG_OPUS", "sampleRateHertz": 24000},
            {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
        ]
        assert all(request.headers["x-goog-api-key"] == "google-tts-test-key" for request in requests)

    def test_google_stream_synthesisiert_jeden_satzblock_einzeln(
        self, google_engine, monkeypatch
    ):
        requested_texts = []
        audio = _fake_ogg_opus()

        def handler(request):
            payload = json.loads(request.content)
            requested_texts.append(payload["input"]["text"])
            return httpx.Response(
                200,
                json={"audioContent": base64.b64encode(audio).decode("ascii")},
            )

        _mock_google(monkeypatch, handler)
        monkeypatch.setattr(tts, "_record_google_usage", lambda _text: None, raising=False)
        text = " ".join(
            f"Das ist Satz Nummer {i} mit einigen zusätzlichen Wörtern."
            for i in range(1, 8)
        )
        expected = tts._split_for_streaming(tts._clean_for_speech(text))

        async def collect():
            return [chunk async for chunk in tts.synthesize_stream(text)]

        chunks = asyncio.run(collect())

        assert requested_texts == [tts._numbers_to_german_words(block) for block in expected]
        assert chunks == [audio] * len(expected)

    def test_api_fehler_faellt_lokal_zurueck_und_loggt_keinen_key(
        self, google_engine, monkeypatch, caplog
    ):
        _fake_local_synthesis(monkeypatch)
        _mock_google(
            monkeypatch,
            lambda request: httpx.Response(500, text="failed google-tts-test-key"),
        )
        caplog.set_level(logging.WARNING, logger=tts.__name__)

        result = asyncio.run(tts.synthesize("Hallo."))

        assert result == b"OGG:KOKORO_WAV:Hallo."
        assert "google-tts-test-key" not in caplog.text

    def test_google_key_macht_tts_ohne_lokale_modelle_verfuegbar(
        self, google_engine, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(tts, "_MODEL_DIR", tmp_path)
        monkeypatch.setattr(tts, "_KOKORO_DIR", tmp_path)

        assert tts.is_available() is True


class TestElevenLabs:
    @pytest.fixture
    def elevenlabs_engine(self, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "elevenlabs", raising=False)
        monkeypatch.setattr(tts.config, "ELEVENLABS_API_KEY", "elevenlabs-synthetic-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ELEVENLABS_MODEL", "eleven_v4_turbo", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ELEVENLABS_VOICE", "iP95p4xoKVk53GoZ742B", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ELEVENLABS_USD_PER_MILLION_CHARS", 0.0, raising=False)
        monkeypatch.setattr(tts.config, "TTS_CARTESIA_PRONUNCIATIONS", {"Mantis": "Mäntis"}, raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["elevenlabs", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts.db, "get_setting", lambda *_args: tts.DEFAULT_VOICE)
        monkeypatch.setattr(tts, "is_available", lambda: True)

    def test_sends_elevenlabs_request_with_spelling_and_records_cost(self, elevenlabs_engine, monkeypatch):
        requests = []
        timeouts = _mock_google(
            monkeypatch,
            lambda request: requests.append(request) or httpx.Response(200, content=b"MP3_AUDIO"),
        )
        monkeypatch.setattr(tts, "_mp3_to_ogg", lambda _audio: _fake_ogg_opus())
        usage = []
        from core import llm_usage
        monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: usage.append((args, kwargs)))

        result = asyncio.run(tts.synthesize("**Mantis** hat 2 Termine."))

        assert result == _fake_ogg_opus()
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert str(request.url) == (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            "iP95p4xoKVk53GoZ742B?output_format=mp3_44100_128"
        )
        assert request.headers["xi-api-key"] == "elevenlabs-synthetic-key"
        assert timeouts == [5.0]
        assert json.loads(request.content) == {
            "text": "Mäntis hat zwei Termine.",
            "model_id": "eleven_v4_turbo",
            "language_code": "de",
        }
        assert usage[0][0][:4] == ("elevenlabs", "eleven_v4_turbo", len("Mäntis hat zwei Termine."), 0)
        assert usage[0][1]["purpose"] == "tts"
        assert usage[0][1]["actual_cost"] == 0

    def test_mp3_konvertierung_lehnt_leere_bytes_direkt_ab(self, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(
            tts.subprocess,
            "run",
            lambda args, **_kwargs: Path(args[-1]).write_bytes(_fake_ogg_opus()),
        )

        with pytest.raises(ValueError, match="leeres MP3-Audio"):
            tts._mp3_to_ogg(b"")

    def test_mp3_konvertierung_lehnt_ogg_ohne_samples_direkt_ab(self, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(
            tts.subprocess,
            "run",
            lambda args, **_kwargs: Path(args[-1]).write_bytes(_fake_ogg_opus(0)),
        )

        with pytest.raises(ValueError, match="ohne Samples"):
            tts._mp3_to_ogg(b"MP3_OHNE_FRAMES")

    def test_mp3_konvertierung_ohne_opus_samples_nutzt_naechste_engine(
        self, elevenlabs_engine, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["elevenlabs", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "_record_elevenlabs_usage", lambda _text: None)
        fallback = []
        expected_audio = _fake_ogg_opus()

        async def mp3(_text):
            return b"MP3_AUDIO"

        async def local(text, _speed):
            fallback.append(text)
            return b"LOCAL_WAV"

        monkeypatch.setattr(tts, "_elevenlabs_synthesize", mp3)
        monkeypatch.setattr(tts, "_mp3_to_ogg", lambda _audio: _fake_ogg_opus(0))
        monkeypatch.setattr(tts, "_synthesize_local", local)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: expected_audio)

        assert asyncio.run(tts.synthesize("Hallo.")) == expected_audio
        assert fallback == ["Hallo."]

    def test_skips_quota_exhausted_engine_without_retrying_every_sentence(
        self, elevenlabs_engine, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-tts-synthetic-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_GOOGLE_VOICE", "de-DE-Chirp3-HD-Charon", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["elevenlabs", "google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_QUOTA_COOLDOWN_S", 123.0, raising=False)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 1000.0)
        requests = []
        expected_audio = _fake_ogg_opus()

        def handler(request):
            requests.append(str(request.url))
            if "elevenlabs.io" in str(request.url):
                return httpx.Response(429, text="quota exceeded")
            return httpx.Response(
                200,
                json={"audioContent": base64.b64encode(expected_audio).decode("ascii")},
            )

        _mock_google(monkeypatch, handler)
        monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)

        for _ in range(2):
            assert asyncio.run(tts.synthesize("Hallo.")) == expected_audio

        assert sum("elevenlabs.io" in url for url in requests) == 1
        assert sum("texttospeech.googleapis.com" in url for url in requests) == 2
        assert tts._elevenlabs_paused_until == 1123.0

    def test_retries_bound_engine_once_with_short_timeout_then_switches_voice(self, monkeypatch, caplog):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "is_available", lambda: True)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)
        monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)
        monkeypatch.setattr(tts, "_BOUND_ENGINE_RETRY_TIMEOUT_S", 0.01, raising=False)
        caplog.set_level(logging.INFO, logger=tts.__name__)
        requested = []
        cartesia_calls = 0
        retry_cancelled = []
        google_audio = _fake_ogg_opus()

        async def cartesia(text):
            nonlocal cartesia_calls
            cartesia_calls += 1
            requested.append("cartesia")
            if cartesia_calls == 1:
                return b"WAV:cartesia"
            if cartesia_calls == 2:
                raise httpx.ReadTimeout("timeout")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                retry_cancelled.append(True)
                raise

        async def google(text):
            requested.append("google")
            return google_audio, False

        monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts, "_google_tts_synthesize", google)
        monkeypatch.setattr(tts, "_cartesia_wav_to_ogg", lambda _wav: b"OggS:cartesia")
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OggS:" + wav)
        async def collect():
            state, token = tts._start_tts_turn()
            try:
                chunks = []
                for sentence in ("Erster Satz.", "Zweiter Satz.", "Dritter Satz."):
                    chunks.extend([chunk async for chunk in tts.synthesize_stream(sentence)])
                return chunks, state.engine
            finally:
                tts._end_tts_turn(token)

        async def bounded_collect():
            return await asyncio.wait_for(collect(), timeout=0.5)

        assert asyncio.run(bounded_collect()) == (
            [b"OggS:cartesia", google_audio, google_audio],
            "google",
        )
        assert requested == ["cartesia", "cartesia", "cartesia", "google", "google"]
        assert retry_cancelled == [True]
        assert "Stimmwechsel im laufenden Turn: Cartesia → Google Cloud" in caplog.text

    def test_gebundene_engine_bleibt_nach_erfolgreichem_retry_im_turn_nutzbar(
        self, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)
        calls = []

        async def cartesia(_text):
            calls.append(True)
            if len(calls) == 2:
                raise httpx.ReadTimeout("kurzer Fehler")
            return b"WAV:cartesia"

        monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts, "_cartesia_wav_to_ogg", lambda _wav: b"OGG:cartesia")

        async def collect():
            state, token = tts._start_tts_turn()
            try:
                chunks = []
                for sentence in ("Erster Satz.", "Zweiter Satz.", "Dritter Satz."):
                    chunks.extend([chunk async for chunk in tts.synthesize_stream(sentence)])
                return chunks, state
            finally:
                tts._end_tts_turn(token)

        chunks, state = asyncio.run(collect())

        assert chunks == [b"OGG:cartesia"] * 3
        assert len(calls) == 4
        assert state.audio_emitted is True
        assert tts._engine_is_paused("cartesia") is False

    def test_gebundene_engine_uebergeht_nur_den_kurzen_fehler_cooldown(
        self, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_QUOTA_COOLDOWN_S", 3600.0, raising=False)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 1000.0)
        state = tts.TTSTurnState(engine="cartesia", chain_position=0, audio_emitted=True)
        token = tts._active_tts_turn.set(state)
        try:
            tts._pause_engine("cartesia", "ReadTimeout", quota=False)
            assert tts.is_available() is True
            assert tts._cartesia_paused_until == 1060.0

            tts._pause_engine("cartesia", 429, quota=True)
            assert tts.is_available() is False
            assert tts._cartesia_paused_until == 4600.0
        finally:
            tts._active_tts_turn.reset(token)

    def test_gebundene_engine_wird_bei_quota_cooldown_nicht_erneut_aufgerufen(
        self, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
        monkeypatch.setattr(
            tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "google"], raising=False
        )
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts.config, "TTS_QUOTA_COOLDOWN_S", 3600.0, raising=False)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)
        monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)
        calls = []
        google_audio = _fake_ogg_opus()
        request = httpx.Request("POST", "https://cartesia.test")
        quota_error = httpx.HTTPStatusError(
            "quota exhausted",
            request=request,
            response=httpx.Response(429, request=request, text="quota exceeded"),
        )

        async def cartesia(_text):
            calls.append("cartesia")
            if calls.count("cartesia") == 2:
                raise quota_error
            return b"WAV:cartesia"

        async def google(_text):
            calls.append("google")
            return google_audio, False

        monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts, "_google_tts_synthesize", google)
        monkeypatch.setattr(tts, "_cartesia_wav_to_ogg", lambda _wav: b"OGG:cartesia")

        async def collect():
            state, token = tts._start_tts_turn()
            try:
                chunks = []
                for sentence in ("Erster Satz.", "Zweiter Satz."):
                    chunks.extend([chunk async for chunk in tts.synthesize_stream(sentence)])
                return chunks
            finally:
                tts._end_tts_turn(token)

        assert asyncio.run(collect()) == [b"OGG:cartesia", google_audio]
        assert calls == ["cartesia", "cartesia", "google"]
        assert tts._cartesia_paused_until == 4600.0

    def test_erschoepfter_retry_kehrt_im_selben_turn_nicht_zur_hauptengine_zurueck(
        self, monkeypatch
    ):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "is_available", lambda: True)
        monkeypatch.setattr(tts, "_record_cartesia_usage", lambda _text: None)
        cartesia_calls = []
        google_calls = []
        local_calls = []

        async def cartesia(_text):
            cartesia_calls.append(True)
            if len(cartesia_calls) == 1:
                return b"WAV:cartesia"
            raise httpx.ReadTimeout("Cartesia timeout")

        async def google(_text):
            google_calls.append(True)
            raise httpx.ReadTimeout("Google timeout")

        async def local(_text, _speed):
            local_calls.append(True)
            raise RuntimeError("local unavailable")

        monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts, "_google_tts_synthesize", google)
        monkeypatch.setattr(tts, "_synthesize_local", local)
        monkeypatch.setattr(tts, "_cartesia_wav_to_ogg", lambda _wav: b"OggS:cartesia")

        async def collect():
            _state, token = tts._start_tts_turn()
            try:
                chunks = []
                for sentence in ("Erster Satz.", "Zweiter Satz.", "Dritter Satz."):
                    chunks.extend([chunk async for chunk in tts.synthesize_stream(sentence)])
                return chunks
            finally:
                tts._end_tts_turn(token)

        assert asyncio.run(collect()) == [b"OggS:cartesia"]
        assert len(cartesia_calls) == 3
        assert len(google_calls) == 1
        assert len(local_calls) == 2

    def test_spaeterer_kurzer_fehler_verkuerzt_quota_cooldown_nicht(self, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_QUOTA_COOLDOWN_S", 3600.0, raising=False)
        now = [1000.0]
        monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])

        tts._pause_engine("google", 429, quota=True)
        now[0] = 1500.0
        tts._pause_engine("google", "ReadTimeout", quota=False)

        assert tts._engine_paused_until["google"] == 4600.0
        assert tts._google_tts_paused_until == 4600.0

    @pytest.mark.parametrize(
        ("engine", "status", "body"),
        [
            ("google", 429, ""),
            ("elevenlabs", 429, "temporarily unavailable"),
            ("google", 403, '{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
            ("cartesia", 402, ""),
        ],
    )
    def test_quota_signale_pausieren_engine_eine_stunde(self, monkeypatch, engine, status, body):
        monkeypatch.setattr(tts.config, "TTS_QUOTA_COOLDOWN_S", 3600.0, raising=False)
        monkeypatch.setattr(tts.time, "monotonic", lambda: 100.0)
        request = httpx.Request("POST", "https://tts.test/synthesize")
        response = httpx.Response(status, request=request, text=body)
        error = httpx.HTTPStatusError("provider error", request=request, response=response)

        assert tts._is_quota_error(engine, error) is True
        tts._pause_engine(engine, status, quota=tts._is_quota_error(engine, error))

        assert tts._engine_paused_until[engine] == 3700.0

    def test_turn_advances_forward_and_does_not_return_to_an_earlier_provider(self, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "cartesia", raising=False)
        monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "cartesia-key", raising=False)
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "google", "local"], raising=False)
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
        monkeypatch.setattr(tts, "is_available", lambda: True)
        calls = []
        local = []
        google_audio = _fake_ogg_opus()

        async def cartesia(_text):
            calls.append("cartesia")
            raise httpx.ReadTimeout("Cartesia timeout")

        async def google(_text):
            calls.append("google")
            if calls.count("google") == 1:
                return google_audio, False
            # Simuliere, dass Cartesias Cooldown später ausläuft, während Google
            # im selben Turn gerade fehlschlägt.
            tts._engine_paused_until["cartesia"] = 0.0
            tts._cartesia_paused_until = 0.0
            raise httpx.ReadTimeout("Google timeout")

        async def local_fallback(text, _speed):
            local.append(text)
            return b"LOCAL_WAV"

        monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts, "_google_tts_synthesize", google)
        monkeypatch.setattr(tts, "_synthesize_local", local_fallback)
        monkeypatch.setattr(tts, "_wav_to_ogg", lambda wav: b"OggS:" + wav)

        async def collect():
            _state, token = tts._start_tts_turn()
            try:
                chunks = []
                for sentence in ("Erster Satz.", "Zweiter Satz.", "Dritter Satz."):
                    chunks.extend([chunk async for chunk in tts.synthesize_stream(sentence)])
                return chunks
            finally:
                tts._end_tts_turn(token)

        assert asyncio.run(collect()) == [
            google_audio, b"OggS:LOCAL_WAV", b"OggS:LOCAL_WAV",
        ]
        assert calls == ["cartesia", "google", "google", "google"]
        assert len(local) == 2

    def test_legacy_engine_overrides_default_chain_when_no_chain_is_configured(self, monkeypatch):
        monkeypatch.setattr(tts.config, "TTS_ENGINE", "google", raising=False)
        monkeypatch.setattr(
            tts.config,
            "TTS_ENGINE_CHAIN",
            ["cartesia", "elevenlabs", "google", "local"],
            raising=False,
        )
        monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", False, raising=False)

        assert tts._engine_chain() == ["google", "cartesia", "elevenlabs", "local"]

    def test_filler_synthesis_never_uses_a_fallback_voice(self, elevenlabs_engine, monkeypatch):
        monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
        requested = []

        async def fail_primary(text):
            requested.append("elevenlabs")
            raise httpx.ReadTimeout("slow")

        async def fallback(text):
            requested.append("google")
            return _fake_ogg_opus(), False

        monkeypatch.setattr(tts, "_elevenlabs_synthesize", fail_primary)
        monkeypatch.setattr(tts, "_google_tts_synthesize", fallback)
        monkeypatch.setattr(tts, "is_available", lambda: True)

        assert asyncio.run(tts.synthesize_filler("Mhm.")) == b""
        assert requested == ["elevenlabs"]


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
        assert tts._cartesia_is_paused()
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
        assert chunks[1:] == [b"OGG:KOKORO_WAV:" + block.encode() for block in expected[1:]]
        assert chunks[0] == b"OGG:WAV:" + expected[0].encode()
        assert tts._cartesia_is_paused()


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


def test_phone_number_before_comma():
    from core import tts
    assert tts._numbers_to_german_words("Ruf 0911 0123456, dann weiter.").startswith("Ruf null neun eins eins, null eins zwei drei vier fünf sechs,")
