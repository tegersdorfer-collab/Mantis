import json
import wave
from datetime import datetime, timezone

import pytest


def test_recorded_segment_is_mono_16khz_wav_with_json_metadata(tmp_path):
    from core.voice_segments import record_voice_segment

    timestamp = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    audio = b"\x01\x00\x02\x00" * 12
    metadata = {
        "transcript": "Mantis, hallo",
        "partial_transcripts": [{"offset_ms": 400, "text": "hallo"}],
        "wakeword_position_ms": 100,
        "silence_used_ms": 288,
        "turn_model_p": 0.87,
        "heuristic_class": "COMPLETE",
    }

    wav_path, json_path = record_voice_segment(
        audio, metadata, root_dir=tmp_path, now=timestamp, max_files=300,
    )

    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.readframes(wav_file.getnframes()) == audio
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["transcript"] == "Mantis, hallo"
    assert payload["sample_rate"] == 16_000
    assert payload["timestamp"] == timestamp.isoformat()
    assert payload["partial_transcripts"] == metadata["partial_transcripts"]


def test_segment_retention_deletes_oldest_wav_and_json_pairs(tmp_path):
    from core.voice_segments import record_voice_segment

    for day in (15, 16, 17):
        record_voice_segment(
            b"\0\0" * 4,
            {"transcript": str(day)},
            root_dir=tmp_path,
            now=datetime(2026, 10, day, tzinfo=timezone.utc),
            max_files=2,
        )

    json_paths = sorted(tmp_path.rglob("*.json"))
    wav_paths = sorted(tmp_path.rglob("*.wav"))

    assert [json.loads(path.read_text())["transcript"] for path in json_paths] == ["17"]
    assert len(wav_paths) == 1
    assert len(json_paths) == 1


def test_recording_rejects_incomplete_pcm_sample(tmp_path):
    from core.voice_segments import record_voice_segment

    with pytest.raises(ValueError, match="16-Bit-PCM"):
        record_voice_segment(b"\0", {}, root_dir=tmp_path)
