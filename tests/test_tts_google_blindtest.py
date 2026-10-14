import asyncio
import base64
import json

import httpx


def test_blindtest_checks_male_voices_and_writes_mp3_and_latency(tmp_path):
    from scripts import tts_blindtest_google as blindtest

    requested = []
    voice_names = ["Charon", "Fenrir", "Orus", "Puck", "Achird"]
    voices = [
        {
            "name": f"de-DE-Chirp3-HD-{name}",
            "ssmlGender": "MALE",
            "languageCodes": ["de-DE"],
            "naturalSampleRateHertz": 24000,
        }
        for name in voice_names
    ]
    voices.append(
        {
            "name": "de-DE-Chirp3-HD-Puck",
            "ssmlGender": "FEMALE",
            "languageCodes": ["de-DE"],
            "naturalSampleRateHertz": 24000,
        }
    )

    def handler(request):
        requested.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"voices": voices})
        return httpx.Response(
            200,
            json={"audioContent": base64.b64encode(b"ID3-mp3").decode("ascii")},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        results = asyncio.run(
            blindtest.run_blindtest(
                tmp_path,
                "google-tts-script-test-key",
                client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(requested) == 1 + len(voice_names) * len(blindtest.SENTENCES)
    assert requested[0].method == "GET"
    assert requested[0].url.path == "/v1/voices"
    assert dict(requested[0].url.params) == {"languageCode": "de-DE"}
    assert all(request.headers["x-goog-api-key"] == "google-tts-script-test-key" for request in requested)
    synth_requests = requested[1:]
    assert all(request.method == "POST" for request in synth_requests)
    assert all(
        json.loads(request.content)["audioConfig"] == {
            "audioEncoding": "MP3",
            "sampleRateHertz": 24000,
        }
        for request in synth_requests
    )
    assert all(path.suffix == ".mp3" for path in tmp_path.glob("*.mp3"))
    assert len(list(tmp_path.glob("*.mp3"))) == len(voice_names) * len(blindtest.SENTENCES)
    assert (tmp_path / "latency.json").exists()
    latency_data = json.loads((tmp_path / "latency.json").read_text())
    assert set(latency_data["voices"]) == {f"de-DE-Chirp3-HD-{name}" for name in voice_names}
    assert all(len(rows) == len(blindtest.SENTENCES) for rows in latency_data["voices"].values())
    assert "google-tts-script-test-key" not in json.dumps(results)
