"""Testet POST /api/voice/segment über einen echten FastAPI-TestClient.
Whisper/Fast-LLM/Agent/TTS werden gemockt — dieser Test prüft nur die
Endpunkt-Verkabelung (Phase 5b: Agent-Anbindung + TTS-Antwort)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base64
import io
import wave
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.voice as voice
from web.routers.voice import build_router


@pytest.fixture(autouse=True)
def _reset_conversation_window():
    """voice_segment() ruft bei Adressierung mark_conversation_active() echt auf —
    ohne Reset würde das den globalen Status über Testgrenzen hinweg verschmutzen."""
    voice._conversation_active_until = 0.0
    yield
    voice._conversation_active_until = 0.0


def _make_client(orch=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(orch))
    return TestClient(app)


def _fake_wav_bytes() -> bytes:
    """Erzeugt eine winzige, gültige (stille) WAV-Datei — reicht für den
    Upload-Pfad-Test, ohne echtes Audio-Material zu brauchen."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 1600)  # 0.1s Stille
    return buf.getvalue()


class TestVoiceSegmentEndpoint:
    def test_adressiert_ruft_agent_auf_und_liefert_antwort_plus_audio(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock(return_value=("Dein Schlaf war gut.", []))
        with patch(
            "core.voice_fillers.choose_cached_filler",
            side_effect=AssertionError("HTTP-Segmente dürfen keine Füller lesen"),
        ), \
             patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="Wie war mein Schlaf?")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)), \
             patch("web.routers.voice.synthesize", new=AsyncMock(return_value=b"FAKE_OGG_BYTES")):
            client = _make_client(fake_orch)
            resp = client.post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "Wie war mein Schlaf?"
        assert body["addressed"] is True
        assert body["reply"] == "Dein Schlaf war gut."
        assert base64.b64decode(body["audio_b64"]) == b"FAKE_OGG_BYTES"
        fake_orch.voice_respond.assert_awaited_once_with("Wie war mein Schlaf?")

    def test_nicht_adressiert_ruft_agent_nicht_auf(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock()
        with patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="Und dann meinte er zu mir...")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=False)), \
             patch("web.routers.voice.synthesize", new=AsyncMock()) as mock_synth:
            client = _make_client(fake_orch)
            resp = client.post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["addressed"] is False
        assert body["reply"] is None
        assert body["audio_b64"] is None
        fake_orch.voice_respond.assert_not_called()
        mock_synth.assert_not_called()

    def test_leeres_transkript_ueberspringt_alles(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock()
        with patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock()) as mock_addr:
            client = _make_client(fake_orch)
            resp = client.post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )
        assert resp.status_code == 200
        assert resp.json() == {"text": "", "addressed": False, "reply": None, "audio_b64": None}
        mock_addr.assert_not_called()
        fake_orch.voice_respond.assert_not_called()

    def test_ohne_orchestrator_liefert_text_ohne_agent_antwort(self):
        """Wenn kein orch verbunden ist (z.B. Startup-Race), darf der Endpunkt nicht crashen."""
        with patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="Wie war mein Schlaf?")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)):
            client = _make_client(orch=None)
            resp = client.post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["addressed"] is True
        assert body["reply"] is None
        assert body["audio_b64"] is None

    def test_tts_fehler_liefert_reply_ohne_audio(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock(return_value=("Dein Schlaf war gut.", []))
        with patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="Wie war mein Schlaf?")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)), \
             patch("web.routers.voice.synthesize", new=AsyncMock(return_value=b"")):
            client = _make_client(fake_orch)
            resp = client.post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "Dein Schlaf war gut."
        assert body["audio_b64"] is None

    def test_lokaler_voice_reflex_markiert_http_latenz(self, monkeypatch):
        from core import voice_timing

        logged = []
        fake_orch = MagicMock()

        async def reflex(_text):
            voice_timing.mark_reflex()
            return "Es ist 09:07 Uhr.", [{"tool": "voice_reflex", "reflex": True}]

        fake_orch.voice_respond = AsyncMock(side_effect=reflex)
        monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
        with patch("web.routers.voice.transcribe_audio", new=AsyncMock(return_value="Wie spät ist es?")), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)), \
             patch("web.routers.voice.synthesize", new=AsyncMock(return_value=b"FAKE_OGG_BYTES")):
            response = _make_client(fake_orch).post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )

        assert response.status_code == 200
        assert logged[0][2]["reflex"] is True
