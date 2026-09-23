import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64

from fastapi.testclient import TestClient
from unittest.mock import patch

from web.routers.voice import build_router, sende_antwort
from fastapi import FastAPI


def make_client():
    app = FastAPI()
    app.include_router(build_router(orch=None))
    return TestClient(app)


class TestStreamModeEndpoint:
    def test_defaults_to_http(self):
        client = make_client()
        with patch("web.routers.voice.db.get_setting", return_value=None):
            resp = client.get("/api/voice/stream-mode")
        assert resp.json() == {"mode": "http"}

    def test_returns_websocket_when_set(self):
        client = make_client()
        with patch("web.routers.voice.db.get_setting", return_value="websocket"):
            resp = client.get("/api/voice/stream-mode")
        assert resp.json() == {"mode": "websocket"}



class FakeWebSocket:
    """Sammelt, was der Server sendet — ohne echten Socket, damit kein Test blockiert."""

    def __init__(self):
        self.gesendet = []

    async def send_json(self, data):
        self.gesendet.append(data)


class TestSendeAntwort:
    """Der Ton wird blockweise geschickt, sobald er entsteht — statt erst am Ende.

    Abwärtskompatibilität ist Pflicht: iOS-Apps und /api/voice/segment kennen
    audio_chunk/audio_end nicht und müssen weiter audio_b64 bekommen.
    """

    def _stream(self, *chunks):
        async def fake(text, **kw):
            for c in chunks:
                yield c
        return patch("web.routers.voice.synthesize_stream", fake)

    def test_schickt_bloecke_einzeln(self):
        ws = FakeWebSocket()
        with self._stream(b"OGG-A", b"OGG-B"):
            asyncio.run(sende_antwort(ws, "wie ist das wetter", "Es wird warm. Und sonnig.", True))
        kopf, a, b, ende = ws.gesendet
        assert kopf["audio_streaming"] is True and kopf["audio_b64"] is None
        assert (a["type"], a["seq"], base64.b64decode(a["b64"])) == ("audio_chunk", 0, b"OGG-A")
        assert (b["seq"], base64.b64decode(b["b64"])) == (1, b"OGG-B")
        assert ende == {"type": "audio_end", "count": 2}

    def test_ohne_streaming_bleibt_es_beim_alten_protokoll(self):
        """Alte Clients (iOS) dürfen sich nicht ändern."""
        ws = FakeWebSocket()
        with patch("web.routers.voice.synthesize", return_value=b"KOMPLETT"):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", False))
        assert len(ws.gesendet) == 1
        antwort = ws.gesendet[0]
        assert base64.b64decode(antwort["audio_b64"]) == b"KOMPLETT"
        assert "audio_streaming" not in antwort

    def test_audio_end_kommt_auch_wenn_tts_scheitert(self):
        """Ohne audio_end bliebe der Client dauerhaft stummgeschaltet."""
        ws = FakeWebSocket()

        async def kaputt(text, **kw):
            raise RuntimeError("TTS tot")
            yield b""  # pragma: no cover

        with patch("web.routers.voice.synthesize_stream", kaputt):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", True))
        assert ws.gesendet[-1] == {"type": "audio_end", "count": 0}

    def test_audio_end_auch_wenn_mitten_im_stream_abbricht(self):
        """Schon gesendete Blöcke bleiben gültig, der Client wird trotzdem entsperrt."""
        ws = FakeWebSocket()

        async def haelftig(text, **kw):
            yield b"OGG-A"
            raise RuntimeError("abgebrochen")

        with patch("web.routers.voice.synthesize_stream", haelftig):
            asyncio.run(sende_antwort(ws, "hallo", "Antwort.", True))
        assert ws.gesendet[-1] == {"type": "audio_end", "count": 1}

    def test_ohne_antworttext_kein_streaming(self):
        ws = FakeWebSocket()
        asyncio.run(sende_antwort(ws, "nicht adressiert", None, True))
        assert ws.gesendet == [{"text": "nicht adressiert", "addressed": True,
                                "reply": None, "audio_b64": None}]
