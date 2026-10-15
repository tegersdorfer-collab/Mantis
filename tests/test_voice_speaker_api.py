"""Enrollment-API verwendet dieselbe geschützte Router-Schicht wie Voice."""
import io
import wave

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.voice import build_router


def _wav(seconds=20):
    pcm = np.full(16_000 * seconds, 1000, dtype="<i2").tobytes()
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(pcm)
    return target.getvalue()


class FakeSpeakerService:
    def __init__(self):
        self.reset_called = False
        self.received = None

    async def status(self):
        return {"enrolled": True, "embeddings": 3, "last_learning": []}

    async def enroll_wavs(self, payloads):
        self.received = payloads
        return {"enrolled": True, "embeddings": len(payloads), "last_learning": []}

    async def reset(self):
        self.reset_called = True


def _client(service, monkeypatch):
    monkeypatch.setattr("core.speaker.get_speaker_verifier", lambda: service)
    app = FastAPI()
    app.include_router(build_router(orch=None))
    return TestClient(app)


def test_speaker_status_returns_profile_state(monkeypatch):
    service = FakeSpeakerService()
    response = _client(service, monkeypatch).get("/api/voice/speaker")

    assert response.status_code == 200
    assert response.json() == {"enrolled": True, "embeddings": 3, "last_learning": []}


def test_speaker_enrollment_accepts_multiple_wav_pieces(monkeypatch):
    service = FakeSpeakerService()
    response = _client(service, monkeypatch).post(
        "/api/voice/speaker/enroll",
        files=[
            ("audio", ("satz-1.wav", _wav(10), "audio/wav")),
            ("audio", ("satz-2.wav", _wav(10), "audio/wav")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["embeddings"] == 2
    assert len(service.received) == 2


def test_speaker_enrollment_translates_short_recording_to_bad_request(monkeypatch):
    class ShortSpeakerService(FakeSpeakerService):
        async def enroll_wavs(self, _payloads):
            raise ValueError("Mindestens 20 Sekunden Sprache erforderlich")

    response = _client(ShortSpeakerService(), monkeypatch).post(
        "/api/voice/speaker/enroll",
        files=[("audio", ("kurz.wav", _wav(1), "audio/wav"))],
    )

    assert response.status_code == 400
    assert "20 Sekunden" in response.json()["detail"]


def test_speaker_delete_resets_profile(monkeypatch):
    service = FakeSpeakerService()
    response = _client(service, monkeypatch).delete("/api/voice/speaker")

    assert response.status_code == 200
    assert response.json() == {"enrolled": False}
    assert service.reset_called
