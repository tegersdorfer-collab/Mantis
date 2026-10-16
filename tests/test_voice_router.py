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

from web.routers.voice import build_router


@pytest.fixture(autouse=True)
def _disable_speaker_verification_for_legacy_cases(monkeypatch):
    monkeypatch.setattr("config.VOICE_SPEAKER_VERIFY", False)


def _make_client(orch=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(orch))
    return TestClient(app)


def _fake_wav_bytes(seconds: float = 1.0) -> bytes:
    """Erzeugt eine winzige, gültige (stille) WAV-Datei — reicht für den
    Upload-Pfad-Test, ohne echtes Audio-Material zu brauchen."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\xe8\x03" * int(16000 * seconds))
    return buf.getvalue()


class TestVoiceSegmentEndpoint:
    def test_adressiert_ruft_agent_auf_und_liefert_antwort_plus_audio(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock(return_value=("Dein Schlaf war gut.", []))
        with patch(
            "core.voice_fillers.choose_cached_filler",
            side_effect=AssertionError("HTTP-Segmente dürfen keine Füller lesen"),
        ), \
             patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Wie war mein Schlaf?", False))), \
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
        fake_orch.voice_respond.assert_awaited_once_with(
            "Wie war mein Schlaf?", authorization_type="addressed",
        )

    def test_nicht_adressiert_ruft_agent_nicht_auf(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock()
        with patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Und dann meinte er zu mir...", False))), \
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

    def test_http_segment_rejects_foreign_speaker_before_agent(self, monkeypatch):
        import numpy as np

        monkeypatch.setattr("config.VOICE_SPEAKER_VERIFY", True)
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock()
        verified_audio = []

        class FakeSpeakerVerifier:
            async def has_profile(self):
                return True

            async def verify_pcm(self, pcm_audio):
                verified_audio.append(pcm_audio)
                return type(
                    "Verification", (),
                    {"decision": "rejected", "score": 0.1, "embedding": np.ones(192)},
                )()

        with patch("core.speaker.get_speaker_verifier", return_value=FakeSpeakerVerifier()), \
             patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Wie war mein Schlaf?", False))), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)) as address_check, \
             patch("web.routers.voice.synthesize", new=AsyncMock()) as mock_synth:
            response = _make_client(fake_orch).post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )

        assert response.status_code == 200
        assert response.json()["addressed"] is False
        assert response.json()["reply"] is None
        assert verified_audio == [b"\xe8\x03" * 16_000]
        address_check.assert_not_called()
        fake_orch.voice_respond.assert_not_called()
        mock_synth.assert_not_called()

    def test_segment_endpoint_prueft_rohes_transkript_ohne_akustischen_wakeword_treffer(self):
        raw = "Mantas, wie spät ist es?"
        address_check = AsyncMock(return_value=False)
        transcriber = AsyncMock(return_value=(raw, False))
        with patch("web.routers.voice.transcribe_audio_with_hint", new=transcriber), \
             patch("web.routers.voice.is_addressed_to_mantis", new=address_check):
            response = _make_client().post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )

        assert response.status_code == 200
        assert response.json()["text"] == raw
        assert response.json()["addressed"] is False
        transcriber.assert_awaited_once()
        assert transcriber.await_args.kwargs == {"wakeword_triggered": False}
        address_check.assert_awaited_once_with(raw, wakeword_hint=False)

    def test_leeres_transkript_ueberspringt_alles(self):
        fake_orch = MagicMock()
        fake_orch.voice_respond = AsyncMock()
        with patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("", False))), \
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
        with patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Wie war mein Schlaf?", False))), \
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
        with patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Wie war mein Schlaf?", False))), \
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

        async def reflex(_text, **_kwargs):
            voice_timing.mark_reflex()
            return "Es ist 09:07 Uhr.", [{"tool": "voice_reflex", "reflex": True}]

        fake_orch.voice_respond = AsyncMock(side_effect=reflex)
        monkeypatch.setattr("web.routers.voice.db.log_event", lambda *args: logged.append(args))
        with patch("web.routers.voice.transcribe_audio_with_hint", new=AsyncMock(return_value=("Wie spät ist es?", False))), \
             patch("web.routers.voice.is_addressed_to_mantis", new=AsyncMock(return_value=True)), \
             patch("web.routers.voice.synthesize", new=AsyncMock(return_value=b"FAKE_OGG_BYTES")):
            response = _make_client(fake_orch).post(
                "/api/voice/segment",
                files={"audio": ("segment.wav", _fake_wav_bytes(), "audio/wav")},
            )

        assert response.status_code == 200
        assert logged[0][2]["reflex"] is True
