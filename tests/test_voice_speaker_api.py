"""Enrollment-API verwendet dieselbe geschützte Router-Schicht wie Voice."""
import io
import asyncio
import wave
from uuid import uuid4

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from web.routers.voice import (
    SPEAKER_ENROLL_MAX_BYTES, _read_bounded_request_body, build_router,
)


def _wav(seconds=20, sample_rate=16_000, channels=1):
    frames = int(sample_rate * seconds)
    pcm = np.full((frames, channels), 1000, dtype="<i2").tobytes()
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
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


def _upload_clip(client, session_id, payload):
    return client.post(
        "/api/voice/speaker/enroll/clip",
        params={"session_id": str(session_id)},
        content=payload,
        headers={"content-type": "audio/wav"},
    )


def test_speaker_status_returns_profile_state(monkeypatch):
    service = FakeSpeakerService()
    response = _client(service, monkeypatch).get("/api/voice/speaker")

    assert response.status_code == 200
    assert response.json() == {"enrolled": True, "embeddings": 3, "last_learning": []}


def test_speaker_enrollment_uploads_raw_wav_clips_then_finishes(monkeypatch):
    service = FakeSpeakerService()
    client = _client(service, monkeypatch)
    session_id = uuid4()
    first = _wav(10)
    second = _wav(10)

    first_response = _upload_clip(client, session_id, first)
    second_response = _upload_clip(client, session_id, second)
    response = client.post(
        "/api/voice/speaker/enroll/finish",
        params={"session_id": str(session_id)},
    )

    assert first_response.status_code == second_response.status_code == 200
    assert first_response.json() == {"clips": 1}
    assert second_response.json() == {"clips": 2}
    assert response.status_code == 200
    assert response.json()["embeddings"] == 2
    assert service.received == [first, second]


def test_speaker_enrollment_translates_short_recording_to_bad_request(monkeypatch):
    class ShortSpeakerService(FakeSpeakerService):
        async def enroll_wavs(self, _payloads):
            raise ValueError("Mindestens 20 Sekunden Sprache erforderlich")

    service = ShortSpeakerService()
    client = _client(service, monkeypatch)
    session_id = uuid4()
    assert _upload_clip(client, session_id, _wav(1)).status_code == 200

    response = client.post(
        "/api/voice/speaker/enroll/finish",
        params={"session_id": str(session_id)},
    )

    assert response.status_code == 400
    assert "20 Sekunden" in response.json()["detail"]


def test_speaker_enrollment_rejects_thirteenth_clip_before_reading_body(monkeypatch):
    service = FakeSpeakerService()
    client = _client(service, monkeypatch)
    session_id = uuid4()
    for _ in range(12):
        assert _upload_clip(client, session_id, _wav(1)).status_code == 200

    consumed = []

    def body():
        consumed.append("read")
        yield _wav(1)

    response = client.post(
        "/api/voice/speaker/enroll/clip",
        params={"session_id": str(session_id)},
        content=body(),
        headers={"content-type": "audio/wav"},
    )

    assert response.status_code == 413
    assert consumed == []
    assert service.received is None


def test_speaker_enrollment_rejects_more_than_ninety_seconds(monkeypatch):
    service = FakeSpeakerService()
    client = _client(service, monkeypatch)
    session_id = uuid4()
    assert _upload_clip(client, session_id, _wav(60)).status_code == 200

    response = _upload_clip(client, session_id, _wav(31))

    assert response.status_code == 413
    assert service.received is None


def test_speaker_enrollment_keeps_four_megabyte_session_total_limit(monkeypatch):
    service = FakeSpeakerService()
    client = _client(service, monkeypatch)
    session_id = uuid4()
    first = _wav(30, sample_rate=44_100)
    second = _wav(20, sample_rate=44_100)
    assert len(first) < SPEAKER_ENROLL_MAX_BYTES
    assert len(first) + len(second) > SPEAKER_ENROLL_MAX_BYTES
    assert _upload_clip(client, session_id, first).status_code == 200

    response = _upload_clip(client, session_id, second)

    assert response.status_code == 413
    assert service.received is None


def test_speaker_enrollment_rejects_content_length_over_four_megabytes_without_reading(
    monkeypatch,
):
    service = FakeSpeakerService()
    client = _client(service, monkeypatch)
    consumed = []

    def body():
        consumed.append("read")
        yield b"x" * (SPEAKER_ENROLL_MAX_BYTES + 1)

    response = client.post(
        "/api/voice/speaker/enroll/clip",
        params={"session_id": str(uuid4())},
        content=body(),
        headers={
            "content-type": "audio/wav",
            "content-length": str(SPEAKER_ENROLL_MAX_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert consumed == []
    assert service.received is None


def test_speaker_enrollment_stops_consuming_stream_when_clip_limit_is_crossed():
    consumed = []

    def stream_chunks():
        consumed.append("within-limit")
        yield b"x" * SPEAKER_ENROLL_MAX_BYTES
        consumed.append("crosses-limit")
        yield b"x"
        consumed.append("must-not-be-read")
        yield b"unconsumed tail"

    chunks = stream_chunks()

    async def receive():
        try:
            body = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": body, "more_body": True}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/voice/speaker/enroll/clip",
            "query_string": b"",
            "headers": [(b"transfer-encoding", b"chunked")],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        },
        receive,
    )

    async def read_body():
        with pytest.raises(HTTPException) as exc_info:
            await _read_bounded_request_body(request)
        assert exc_info.value.status_code == 413

    asyncio.run(read_body())

    assert consumed == ["within-limit", "crosses-limit"]


def test_speaker_enrollment_times_out_a_slow_stream(monkeypatch):
    import web.routers.voice as voice_router

    monkeypatch.setattr(voice_router, "SPEAKER_ENROLL_UPLOAD_TIMEOUT_SECONDS", 0.001)

    async def receive():
        await asyncio.sleep(0.01)
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/voice/speaker/enroll/clip",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        },
        receive,
    )

    async def read_body():
        with pytest.raises(HTTPException) as exc_info:
            await _read_bounded_request_body(request)
        assert exc_info.value.status_code == 408

    asyncio.run(read_body())


def test_speaker_enrollment_rejects_invalid_wav_without_adding_clip(monkeypatch):
    client = _client(FakeSpeakerService(), monkeypatch)
    session_id = uuid4()

    response = _upload_clip(client, session_id, b"not a wav")

    assert response.status_code == 400
    finish = client.post(
        "/api/voice/speaker/enroll/finish",
        params={"session_id": str(session_id)},
    )
    assert finish.status_code == 404


def test_speaker_delete_resets_profile(monkeypatch):
    service = FakeSpeakerService()
    response = _client(service, monkeypatch).delete("/api/voice/speaker")

    assert response.status_code == 200
    assert response.json() == {"enrolled": False}
    assert service.reset_called
