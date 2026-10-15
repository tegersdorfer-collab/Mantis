"""Tests for the isolated Qwen3-TTS worker and its fallback integration."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import base64
import io
import json
import wave

import pytest

import core.tts as tts


def _write_fake_worker(tmp_path, source):
    worker = tmp_path / "fake_worker.py"
    worker.write_text(source, encoding="utf-8")
    return worker


def _configure_klon(monkeypatch, tmp_path, worker_source):
    ref_dir = tmp_path / "voice_clone"
    ref_dir.mkdir(exist_ok=True)
    (ref_dir / "nico_ref.wav").write_bytes(b"fake reference")
    (ref_dir / "nico_ref.txt").write_text("Referenztext", encoding="utf-8")
    worker = _write_fake_worker(tmp_path, worker_source)
    monkeypatch.setattr(tts, "_KLON_WORKER_PATH", worker, raising=False)
    monkeypatch.setattr(tts, "_klon_process", None)
    monkeypatch.setattr(tts, "_klon_process_lock", asyncio.Lock())
    monkeypatch.setattr(tts, "_klon_idle_task", None)
    monkeypatch.setattr(tts, "_klon_last_used", 0.0)
    monkeypatch.setattr(tts, "_klon_request_seq", 0)
    monkeypatch.setattr(tts.config, "TTS_KLON_PYTHON", sys.executable, raising=False)
    monkeypatch.setattr(tts.config, "TTS_KLON_REF_DIR", str(ref_dir), raising=False)
    monkeypatch.setattr(tts.config, "TTS_KLON_IDLE_S", 600, raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["klon"], raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    return worker, ref_dir


def _pcm_payload(value=0.25, frames=240):
    import struct

    sample = struct.pack("<h", int(value * 32767))
    return base64.b64encode(sample * frames).decode("ascii")


def _fake_ogg():
    pre_skip = 312
    body = b"OpusHead" + bytes((1, 1)) + pre_skip.to_bytes(2, "little") + b"\0" * 7
    granule = pre_skip + 2400
    header = b"OggS\0\0" + granule.to_bytes(8, "little") + b"\0" * 12 + bytes((1,))
    return header + bytes((len(body),)) + body


def _async_worker_cleanup():
    return tts._stop_klon_worker()


def test_klon_pronunciation_uses_whole_words_case_insensitively(monkeypatch):
    monkeypatch.setattr(tts.config, "TTS_KLON_PRONUNCIATIONS", {
        "Branch": "Bräntsch",
        "Branches": "Bräntsches",
        "gemerged": "gemördscht",
        "Spotify": "Spottifei",
    }, raising=False)

    assert tts._apply_klon_pronunciations(
        "Der branch wurde gemerged. Branches, branchweise, SPOTIFY und okay."
    ) == "Der bräntsch wurde gemördscht. Bräntsches, branchweise, SPOTTIFEI und okay."
    assert tts._apply_cartesia_pronunciations("Der branch wurde gemerged.") == (
        "Der branch wurde gemerged."
    )


def test_klon_is_available_only_with_python_worker_and_reference_files(
    monkeypatch, tmp_path,
):
    worker, ref_dir = _configure_klon(monkeypatch, tmp_path, "pass")

    assert tts._engine_available("klon") is True

    (ref_dir / "nico_ref.txt").unlink()
    assert tts._engine_available("klon") is False

    (ref_dir / "nico_ref.txt").write_text("Referenztext", encoding="utf-8")
    worker.unlink()
    assert tts._engine_available("klon") is False


def test_klon_synthesize_collects_pcm_and_records_zero_cost_usage(
    monkeypatch, tmp_path,
):
    requests_file = tmp_path / "requests.jsonl"
    worker_source = f'''import base64, json, sys
requests_path = {str(requests_file)!r}
for line in sys.stdin:
    request = json.loads(line)
    with open(requests_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\\n")
    for seq in range(2):
        print(json.dumps({{"id": request["id"], "seq": seq,
            "pcm_b64": { _pcm_payload()!r }, "sample_rate": 24000}}), flush=True)
    print(json.dumps({{"id": request["id"], "done": True}}), flush=True)
'''
    _configure_klon(monkeypatch, tmp_path, worker_source)
    monkeypatch.setattr(tts.config, "TTS_KLON_PRONUNCIATIONS", {"Branch": "Bräntsch"}, raising=False)
    converted_wavs = []

    def convert(wav_bytes):
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            assert source.getframerate() == 24000
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            converted_wavs.append(source.readframes(source.getnframes()))
        return _fake_ogg()

    monkeypatch.setattr(tts, "_wav_to_ogg", convert)
    usage = []
    monkeypatch.setattr(tts, "_record_klon_usage", lambda text: usage.append(text), raising=False)

    async def run():
        try:
            return await tts.synthesize("Der Branch läuft.")
        finally:
            await _async_worker_cleanup()

    assert asyncio.run(run()) == _fake_ogg()
    request = json.loads(requests_file.read_text(encoding="utf-8").splitlines()[0])
    assert request["text"] == "Der Bräntsch läuft."
    assert request["id"]
    assert len(converted_wavs[0]) == 2 * 240 * 2
    assert usage == ["Der Bräntsch läuft."]


def test_klon_stream_yields_before_worker_finishes_generation(monkeypatch, tmp_path):
    continue_file = tmp_path / "continue"
    worker_source = f'''import base64, json, sys, time
continue_path = {str(continue_file)!r}
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({{"id": request["id"], "seq": 0,
        "pcm_b64": {_pcm_payload(0.2)!r}, "sample_rate": 24000}}), flush=True)
    while not __import__("os").path.exists(continue_path):
        time.sleep(0.01)
    print(json.dumps({{"id": request["id"], "seq": 1,
        "pcm_b64": {_pcm_payload(0.3)!r}, "sample_rate": 24000}}), flush=True)
    print(json.dumps({{"id": request["id"], "done": True}}), flush=True)
'''
    _configure_klon(monkeypatch, tmp_path, worker_source)
    monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: _fake_ogg())
    monkeypatch.setattr(tts, "_record_klon_usage", lambda _text: None, raising=False)

    async def run():
        stream = tts.synthesize_stream("Hallo Klon.")
        try:
            first = await asyncio.wait_for(anext(stream), timeout=2)
            assert first == _fake_ogg()
            assert not continue_file.exists()
            continue_file.touch()
            rest = [chunk async for chunk in stream]
            return [first, *rest]
        finally:
            await stream.aclose()
            await _async_worker_cleanup()

    assert asyncio.run(run()) == [_fake_ogg(), _fake_ogg()]


def test_klon_worker_crash_is_restarted_for_the_next_request(monkeypatch, tmp_path):
    counter_file = tmp_path / "starts"
    worker_source = f'''import base64, json, os, sys
counter_path = {str(counter_file)!r}
for line in sys.stdin:
    request = json.loads(line)
    try:
        count = int(open(counter_path, encoding="utf-8").read()) + 1
    except FileNotFoundError:
        count = 1
    open(counter_path, "w", encoding="utf-8").write(str(count))
    if count == 1:
        sys.exit(0)
    print(json.dumps({{"id": request["id"], "seq": 0,
        "pcm_b64": {_pcm_payload()!r}, "sample_rate": 24000}}), flush=True)
    print(json.dumps({{"id": request["id"], "done": True}}), flush=True)
'''
    _configure_klon(monkeypatch, tmp_path, worker_source)
    monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: _fake_ogg())
    monkeypatch.setattr(tts, "_record_klon_usage", lambda _text: None, raising=False)

    async def run():
        try:
            first = await tts.synthesize("Erster Versuch.")
            second = await tts.synthesize("Zweiter Versuch.")
            return first, second
        finally:
            await _async_worker_cleanup()

    assert asyncio.run(run()) == (b"", _fake_ogg())
    assert counter_file.read_text(encoding="utf-8") == "2"


def test_klon_worker_stops_after_configured_idle_period(monkeypatch, tmp_path):
    worker_source = f'''import base64, json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({{"id": request["id"], "seq": 0,
        "pcm_b64": {_pcm_payload()!r}, "sample_rate": 24000}}), flush=True)
    print(json.dumps({{"id": request["id"], "done": True}}), flush=True)
'''
    _configure_klon(monkeypatch, tmp_path, worker_source)
    monkeypatch.setattr(tts.config, "TTS_KLON_IDLE_S", 0.02, raising=False)
    monkeypatch.setattr(tts, "_wav_to_ogg", lambda _wav: _fake_ogg())
    monkeypatch.setattr(tts, "_record_klon_usage", lambda _text: None, raising=False)

    async def run():
        assert await tts.synthesize("Idle-Test.") == _fake_ogg()
        await asyncio.sleep(0.08)
        assert tts._klon_process is None

    asyncio.run(run())


def test_unavailable_klon_continues_to_the_next_provider(monkeypatch, tmp_path):
    worker, _ref_dir = _configure_klon(monkeypatch, tmp_path, "pass")
    worker.unlink()
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["klon", "google"], raising=False)
    monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)
    monkeypatch.setattr(
        tts,
        "_google_tts_synthesize",
        lambda _text: asyncio.sleep(0, result=(_fake_ogg(), False)),
    )
    monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)

    assert asyncio.run(tts.synthesize("Fallback.")) == _fake_ogg()


def test_klon_empty_audio_is_rejected_before_falling_back(monkeypatch, tmp_path):
    worker_source = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"id": request["id"], "done": True}), flush=True)
'''
    _configure_klon(monkeypatch, tmp_path, worker_source)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["klon", "google"], raising=False)
    monkeypatch.setattr(tts.config, "GOOGLE_TTS_API_KEY", "google-key", raising=False)

    async def google(_text):
        return _fake_ogg(), False

    monkeypatch.setattr(tts, "_google_tts_synthesize", google)
    monkeypatch.setattr(tts, "_record_google_tts_usage", lambda _text: None)

    async def run():
        try:
            return await tts.synthesize("Leer-Test.")
        finally:
            await _async_worker_cleanup()

    assert asyncio.run(run()) == _fake_ogg()


def test_filler_remains_bound_to_cartesia_when_klon_is_next(monkeypatch, tmp_path):
    _configure_klon(monkeypatch, tmp_path, "pass")
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "klon"], raising=False)
    monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "", raising=False)
    monkeypatch.setattr(tts, "is_available", lambda: True)
    monkeypatch.setattr(
        tts,
        "_klon_pcm_stream",
        lambda _text: pytest.fail("Füller dürfen nicht auf Klon wechseln"),
    )

    assert asyncio.run(tts.synthesize_filler("Mhm.")) == b""


def test_klon_usage_records_zero_cost(monkeypatch):
    from core import llm_usage

    records = []
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))

    tts._record_klon_usage("Hallo Klon.")

    args, kwargs = records[0]
    assert args == (
        "klon",
        "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
        len("Hallo Klon."),
        0,
    )
    assert kwargs["purpose"] == "tts"
    assert kwargs["actual_cost"] == 0.0


def test_cartesia_quota_schedules_klon_prewarm(monkeypatch):
    monkeypatch.setattr(tts.config, "CARTESIA_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN", ["cartesia", "klon"], raising=False)
    monkeypatch.setattr(tts.config, "TTS_ENGINE_CHAIN_EXPLICIT", True, raising=False)
    monkeypatch.setattr(tts, "_engine_available", lambda engine: engine == "cartesia")
    monkeypatch.setattr(tts, "_has_provider_key", lambda _engine: True)
    monkeypatch.setattr(tts, "_klon_is_configured", lambda: True)
    prewarmed = []

    async def prewarm():
        prewarmed.append(True)

    monkeypatch.setattr(tts, "_prewarm_klon", prewarm, raising=False)
    request = __import__("httpx").Request("POST", "https://api.cartesia.ai/tts/bytes")
    quota_error = __import__("httpx").HTTPStatusError(
        "quota exceeded",
        request=request,
        response=__import__("httpx").Response(429, request=request, text="quota exceeded"),
    )

    async def cartesia(_text):
        raise quota_error

    monkeypatch.setattr(tts, "_cartesia_synthesize", cartesia)

    async def run():
        await tts._synthesize_provider("cartesia", "Hallo.", 1.0)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert prewarmed == [True]
