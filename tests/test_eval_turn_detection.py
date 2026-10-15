import asyncio
import json
import struct
import wave
from types import SimpleNamespace


def test_offline_eval_maps_heuristic_classes_to_existing_silence_delays():
    from scripts.eval_turn_detection import heuristic_silence_ms

    assert heuristic_silence_ms("Mach das Licht an") == 450
    assert heuristic_silence_ms("Ich wollte fragen ob") == 2000
    assert heuristic_silence_ms("vielleicht später") == 800


def test_checkpoint_audio_keeps_speech_and_appends_the_requested_silence():
    from scripts.eval_turn_detection import checkpoint_audio

    speech = struct.pack("<4h", 100, 200, 300, 400)

    result = checkpoint_audio(speech + b"\0\0" * 2, speech_end_sample=3, silence_ms=100)

    assert result == struct.pack("<3h", 100, 200, 300) + b"\0\0" * 1600


def test_eval_finds_internal_quiet_gaps_only_when_speech_resumes():
    from scripts.eval_turn_detection import internal_quiet_gaps

    frame_samples = 512
    speech = struct.pack("<h", 2000) * frame_samples
    silence = b"\0\0" * frame_samples
    audio = speech * 3 + silence * 8 + speech * 3 + silence * 12

    assert internal_quiet_gaps(audio, speech_end_sample=frame_samples * 14) == [
        (frame_samples * 3, frame_samples * 11),
    ]


def test_internal_pause_heuristic_uses_only_partial_text_available_at_that_time(tmp_path):
    from scripts.eval_turn_detection import evaluate_directory

    frame_samples = 512
    speech = struct.pack("<h", 2000) * frame_samples
    silence = b"\0\0" * frame_samples
    audio = speech * 3 + silence * 8 + speech * 3 + silence * 12
    wav_path = tmp_path / "capture.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(audio)
    wav_path.with_suffix(".json").write_text(json.dumps({
        "last_speech_sample_index": frame_samples * 14,
        "transcript": "Mach das Licht an",
        "partial_text": "Mach das Licht an",
        "partial_transcripts": [
            {"offset_ms": 500, "text": "Mach das Licht an"},
        ],
    }))

    class Analyzer:
        async def predict(self, _audio):
            return SimpleNamespace(probability=0.5)

    rows = asyncio.run(evaluate_directory(tmp_path, Analyzer()))
    internal_pause = next(row for row in rows if row["point"] == "interne_Pause")

    assert internal_pause["heuristic_delay_ms"] == 800
