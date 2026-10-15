import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import struct
from unittest.mock import MagicMock, patch

import pytest

import core.vad as vad


def silence_chunk(n_samples: int = 512) -> bytes:
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


class TestSileroVAD:
    def test_speech_probability_calls_onnx_session(self, tmp_path):
        model_path = tmp_path / "fake.onnx"
        model_path.write_bytes(b"fake")
        with patch.object(vad, "_load_session") as mock_load:
            mock_session = MagicMock()
            mock_session.run.return_value = [[[0.87]]]
            mock_load.return_value = mock_session

            model = vad.SileroVAD(model_path)
            prob = model.speech_probability(silence_chunk())

            assert prob == 0.87
            mock_session.run.assert_called_once()

    def test_state_carries_over_between_calls(self, tmp_path):
        import numpy as np

        model_path = tmp_path / "fake.onnx"
        model_path.write_bytes(b"fake")
        with patch.object(vad, "_load_session") as mock_load:
            mock_session = MagicMock()
            state_output = np.full((2, 1, 128), 0.42, dtype=np.float32)
            mock_session.run.return_value = [np.array([[0.75]]), state_output]
            mock_load.return_value = mock_session

            model = vad.SileroVAD(model_path)
            initial_state = model._state.copy()

            prob1 = model.speech_probability(silence_chunk())
            assert prob1 == 0.75
            # state should have been updated from the initial zeros
            assert not np.array_equal(model._state, initial_state)
            np.testing.assert_array_equal(model._state, state_output)

            prob2 = model.speech_probability(silence_chunk())
            assert prob2 == 0.75

            assert mock_session.run.call_args_list[1].args[1]["state"] is not initial_state
            np.testing.assert_array_equal(
                mock_session.run.call_args_list[1].args[1]["state"], state_output
            )

    def test_prepends_previous_chunk_tail_as_context(self, tmp_path):
        """Regression test for a real bug found via live end-to-end testing (see
        docs/superpowers/plans/2026-07-05-vad-wakeword-streaming.md Task 6): the
        official Silero VAD v5+ ONNX graph requires the last 64 samples of the
        PREVIOUS chunk prepended to the current 512-sample chunk (576 total),
        separately from the recurrent `state` tensor. Without this, the model
        silently returns near-zero speech probability for real speech audio —
        confirmed against the official `silero-vad` PyPI package's
        `OnnxWrapper.__call__` (`context_size = 64` for 16kHz)."""
        import numpy as np

        model_path = tmp_path / "fake.onnx"
        model_path.write_bytes(b"fake")
        with patch.object(vad, "_load_session") as mock_load:
            mock_session = MagicMock()
            mock_session.run.return_value = [np.array([[0.5]])]
            mock_load.return_value = mock_session

            model = vad.SileroVAD(model_path)

            chunk1_samples = [1000] * 512
            chunk1 = struct.pack("<512h", *chunk1_samples)
            model.speech_probability(chunk1)
            first_input = mock_session.run.call_args_list[0].args[1]["input"]
            assert first_input.shape == (1, 512 + vad.SileroVAD.CONTEXT_SAMPLES)
            # Erster Aufruf: Context ist noch der initiale Null-Puffer
            np.testing.assert_array_equal(
                first_input[0, : vad.SileroVAD.CONTEXT_SAMPLES], np.zeros(vad.SileroVAD.CONTEXT_SAMPLES)
            )

            chunk2_samples = [2000] * 512
            chunk2 = struct.pack("<512h", *chunk2_samples)
            model.speech_probability(chunk2)
            second_input = mock_session.run.call_args_list[1].args[1]["input"]
            # Zweiter Aufruf: Context sind die letzten 64 Samples von Chunk 1 (1000/32768.0)
            expected_context = np.full(vad.SileroVAD.CONTEXT_SAMPLES, 1000 / 32768.0, dtype=np.float32)
            np.testing.assert_allclose(
                second_input[0, : vad.SileroVAD.CONTEXT_SAMPLES], expected_context, rtol=1e-5
            )


class FakeVAD:
    """Test-Double: gibt vordefinierte Wahrscheinlichkeiten in Aufrufreihenfolge zurück."""
    def __init__(self, probs: list[float]):
        self._probs = iter(probs)

    def speech_probability(self, pcm_chunk: bytes) -> float:
        return next(self._probs)


class TestVadSegmenter:
    def test_silence_age_and_smart_turn_mode_control_segment_end(self):
        seg = vad.VadSegmenter(FakeVAD([0.9, 0.1, 0.1, 0.9]), chunk_ms=100)
        seg.process_chunk(silence_chunk())
        seg.set_smart_turn_mode("model_incomplete")
        seg.process_chunk(silence_chunk())
        seg.process_chunk(silence_chunk())

        assert seg.current_silence_ms == 200
        assert seg._silence_threshold_ms() == 2000

        seg.process_chunk(silence_chunk())

        assert seg.current_silence_ms == 0
        assert seg.smart_turn_mode is None

    def test_model_complete_turn_ends_on_the_next_silent_frame(self):
        seg = vad.VadSegmenter(FakeVAD([0.9, 0.9, 0.9, 0.1]), chunk_ms=100)
        for _ in range(3):
            seg.process_chunk(silence_chunk())
        seg.set_smart_turn_mode("model_complete")

        event = seg.process_chunk(silence_chunk())

        assert event is not None
        assert event.silence_ms == 100

    def test_incomplete_utterance_uses_two_second_silence_default(self):
        segmenter = vad.VadSegmenter(FakeVAD([]), chunk_ms=32)
        segmenter.set_partial_transcript("Mantis erinnere mich an")
        segmenter._last_speech_sample_index = 0

        assert segmenter._silence_threshold_ms() == 2000

    def test_tracks_current_speech_duration_and_energy_only_for_speech_frames(self):
        seg = vad.VadSegmenter(FakeVAD([0.9, 0.9, 0.1]), chunk_ms=32)
        leise = struct.pack("<512h", *([1000] * 512))
        laut = struct.pack("<512h", *([3000] * 512))
        stille = silence_chunk()

        seg.process_chunk(leise)
        seg.process_chunk(laut)
        seg.process_chunk(stille)

        assert seg.current_speech_ms == 64
        assert seg.current_speech_rms == pytest.approx(((1000**2 + 3000**2) / 2) ** 0.5)

    def test_silence_threshold_uses_config_setting(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 200)
        seg = vad.VadSegmenter(FakeVAD([0.9, 0.9, 0.9, 0.1, 0.1]), chunk_ms=100)

        events = [seg.process_chunk(silence_chunk()) for _ in range(5)]

        assert events[-1] is not None
        assert events[-1].duration_ms == 500

    def test_no_segment_while_silent(self):
        fake = FakeVAD([0.1, 0.1, 0.1])
        seg = vad.VadSegmenter(fake, chunk_ms=100)
        for _ in range(3):
            assert seg.process_chunk(silence_chunk()) is None

    def test_emits_segment_after_speech_then_silence(self):
        # 100ms Chunks: 5x Sprache (500ms) dann 9x Stille (900ms, > SILENCE_MS_TO_STOP=800)
        probs = [0.9] * 5 + [0.1] * 9
        fake = FakeVAD(probs)
        seg = vad.VadSegmenter(fake, chunk_ms=100)
        events = [seg.process_chunk(silence_chunk()) for _ in range(len(probs))]
        emitted = [e for e in events if e is not None]
        assert len(emitted) == 1
        assert emitted[0].duration_ms >= 500

    def test_segment_event_tracks_last_speech_sample_before_silence_padding(self):
        probs = [0.1] * 2 + [0.9] * 10 + [0.1] * 25
        seg = vad.VadSegmenter(FakeVAD(probs), chunk_ms=32)

        emitted = [seg.process_chunk(silence_chunk()) for _ in probs]
        segment = next(event for event in emitted if event is not None)

        assert segment.last_speech_sample_index == 12 * 512
        assert len(segment.audio) // 2 == 37 * 512

    def test_discards_segment_shorter_than_min_duration(self):
        # nur 200ms Sprache (< MIN_SEGMENT_MS=300) dann Stille
        probs = [0.9] * 2 + [0.1] * 9
        fake = FakeVAD(probs)
        seg = vad.VadSegmenter(fake, chunk_ms=100)
        events = [seg.process_chunk(silence_chunk()) for _ in range(len(probs))]
        assert all(e is None for e in events)

    def test_completed_german_utterances_use_fast_silence(self, monkeypatch):
        import config
        from core.vad import utterance_is_complete

        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True)
        monkeypatch.setattr(config, "VOICE_SILENCE_FAST_MS", 450)
        monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 1200)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800)
        completed = [
            "wie spät ist es",
            "mach das Licht an",
            "was steht heute an",
            "Wie wird das Wetter morgen",
            "spiel meine Lieblingsmusik",
            "lies mir die Termine vor",
            "wie war mein Schlaf",
            "kannst du die Aufgabe abhaken",
            "was gibt es heute zum Abendessen",
            "stell den Timer auf zehn Minuten",
        ]

        assert all(utterance_is_complete(text) for text in completed)
        seg = vad.VadSegmenter(FakeVAD([0.9] * 3 + [0.1] * 8), chunk_ms=100)
        for _ in range(3):
            seg.process_chunk(silence_chunk())
        seg.set_partial_transcript("mach das Licht an")
        events = [None] * 3 + [seg.process_chunk(silence_chunk()) for _ in range(8)]
        emitted_at = next(i for i, event in enumerate(events) if event is not None) - 2
        assert emitted_at == 5

    def test_complete_but_stale_partial_keeps_standard_silence(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True)
        monkeypatch.setattr(config, "VOICE_SILENCE_FAST_MS", 450)
        monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 1200)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800)
        monkeypatch.setattr(config, "VOICE_PARTIAL_STALE_MS", 400, raising=False)
        seg = vad.VadSegmenter(FakeVAD([0.9] * 16 + [0.1] * 25), chunk_ms=32)
        for _ in range(16):
            seg.process_chunk(silence_chunk())
        seg.set_partial_transcript("mach das Licht an", audio_end_sample_index=0)

        events = [seg.process_chunk(silence_chunk()) for _ in range(25)]
        emitted_at = next(i for i, event in enumerate(events) if event is not None) + 1

        assert emitted_at == 25

    def test_unfinished_german_utterances_use_slow_silence(self, monkeypatch):
        import config
        from core.vad import utterance_is_complete

        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True)
        monkeypatch.setattr(config, "VOICE_SILENCE_FAST_MS", 450)
        monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 1200)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800)
        unfinished = [
            "erinner mich an",
            "kannst du mir und",
            "ich wollte fragen ob",
            "wenn ich morgen Zeit habe",
            "weil ich heute",
            "stell den Timer auf",
            "ich muss noch",
            "kannst du vielleicht",
            "das neue",
            "und danach",
            "ähm also",
        ]

        assert not any(utterance_is_complete(text) for text in unfinished)
        seg = vad.VadSegmenter(FakeVAD([0.9] * 3 + [0.1] * 13), chunk_ms=100)
        seg.set_partial_transcript("ich wollte fragen ob")
        events = [seg.process_chunk(silence_chunk()) for _ in range(16)]
        emitted_at = next(i for i, event in enumerate(events) if event is not None) - 2
        assert emitted_at == 12

    def test_no_partial_text_keeps_800ms_and_adaptive_setting_can_be_disabled(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800)
        no_partial = vad.VadSegmenter(FakeVAD([0.9] * 3 + [0.1] * 9), chunk_ms=100)
        events = [no_partial.process_chunk(silence_chunk()) for _ in range(12)]
        assert next(i for i, event in enumerate(events) if event is not None) - 2 == 8

        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", False)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 300)
        disabled = vad.VadSegmenter(FakeVAD([0.9] * 3 + [0.1] * 4), chunk_ms=100)
        disabled.set_partial_transcript("mach das Licht an")
        events = [disabled.process_chunk(silence_chunk()) for _ in range(7)]
        assert next(i for i, event in enumerate(events) if event is not None) - 2 == 3


def test_utterance_completion_is_conservative_for_openings_and_fragments():
    from core.vad import utterance_is_complete

    unfinished = [
        "ich würde gerne",
        "kannst du",
        "Mantis kannst du",
        "ich möchte",
        "wie viel",
        "was ist mit",
    ]

    assert [text for text in unfinished if utterance_is_complete(text)] == []


def test_utterance_completion_accepts_explicitly_finished_patterns():
    from core.vad import utterance_is_complete

    completed = [
        "Wie spät ist es?",
        "Was steht heute an?",
        "Wie wird das Wetter morgen?",
        "Mach das Licht an",
        "Schalte den Ventilator aus",
        "Stell den Timer auf zehn Minuten",
        "Kannst du die Aufgabe abhaken",
        "Ich möchte die Aufgabe abhaken",
        "Stopp",
        "Danke",
        "Pause",
        "Weiter",
        "Lauter",
        "Leiser",
    ]

    assert [text for text in completed if not utterance_is_complete(text)] == []


@pytest.mark.parametrize(("text", "expected_state"), [
    ("Wie spät ist es?", "COMPLETE"),
    ("Was steht heute an?", "COMPLETE"),
    ("Wie wird das Wetter morgen?", "COMPLETE"),
    ("Mach das Licht an", "COMPLETE"),
    ("Mach die Musik an", "COMPLETE"),
    ("Schalte das Licht aus", "COMPLETE"),
    ("Schalte das Licht ein", "COMPLETE"),
    ("Schalte den Ventilator ein", "COMPLETE"),
    ("Stell den Timer ein", "COMPLETE"),
    ("Erinner mich an ein", "INCOMPLETE"),
    ("Gib mir ein", "INCOMPLETE"),
    ("Stell den Timer auf ein", "INCOMPLETE"),
    ("Hol mir aus der Küche ein", "INCOMPLETE"),
    ("Lies mir heute ein", "INCOMPLETE"),
    ("Kauf mir ein", "INCOMPLETE"),
    ("Schalte den Fernseher um ein", "INCOMPLETE"),
    ("Schalte das Licht und ein", "INCOMPLETE"),
    ("Stell den Wecker bis ein", "INCOMPLETE"),
    ("Stell dem Kunden ein", "INCOMPLETE"),
    ("Stell der Kundin ein", "INCOMPLETE"),
    ("Stell einen Timer ein", "COMPLETE"),
    ("Schalte unsere Lampen ein", "COMPLETE"),
    ("Schalte diesen Ventilator ein", "COMPLETE"),
    ("Mach das Licht ab", "COMPLETE"),
    ("Mach die Musik lauter", "COMPLETE"),
    ("Lies mal die Nachrichten vor", "COMPLETE"),
    ("Schalte den Ventilator aus", "COMPLETE"),
    ("Stell den Timer auf zehn Minuten", "COMPLETE"),
    ("Kannst du die Aufgabe abhaken", "COMPLETE"),
    ("Ich möchte die Aufgabe abhaken", "COMPLETE"),
    ("Lies mir die Termine vor", "COMPLETE"),
    ("Spiele meine Lieblingsmusik", "COMPLETE"),
    ("Mantis läuft", "COMPLETE"),
    ("Was kostet der Kaffee?", "COMPLETE"),
    ("Danke", "COMPLETE"),
    ("Pause", "COMPLETE"),
    ("Stopp", "COMPLETE"),
    ("Weiter", "COMPLETE"),
    ("Lauter", "COMPLETE"),
    ("Leiser", "COMPLETE"),
    ("spiel meine Lieblingsmusik", "COMPLETE"),
    ("wie war mein Schlaf", "COMPLETE"),
    ("was gibt es heute zum Abendessen", "COMPLETE"),
    ("Mach den Branch Merge", "INCOMPLETE"),
    ("Schalte das Licht", "INCOMPLETE"),
    ("Mach das Licht", "INCOMPLETE"),
    ("Dreh die Musik", "INCOMPLETE"),
    ("Mach mal", "INCOMPLETE"),
    ("Lies mal", "INCOMPLETE"),
    ("Mach mal kurz", "INCOMPLETE"),
    ("Mach Licht", "UNKNOWN"),
    ("Wie geht es dir", "COMPLETE"),
    ("Was meinst du", "COMPLETE"),
    ("Wer bist du", "COMPLETE"),
    ("Was ist das", "COMPLETE"),
    ("Danke dir", "UNKNOWN"),
    ("Kannst du mir helfen", "COMPLETE"),
    ("Ich würde gerne", "INCOMPLETE"),
    ("Kannst du", "INCOMPLETE"),
    ("Mantis kannst du", "INCOMPLETE"),
    ("Ich möchte", "INCOMPLETE"),
    ("Wie viel", "INCOMPLETE"),
    ("Was ist mit", "INCOMPLETE"),
    ("Wie kann ich am besten", "INCOMPLETE"),
    ("Was ist der beste", "INCOMPLETE"),
    ("Erinner mich an", "INCOMPLETE"),
    ("Kannst du mir und", "INCOMPLETE"),
    ("Ich wollte fragen ob", "INCOMPLETE"),
    ("Wenn ich morgen Zeit habe", "INCOMPLETE"),
    ("Weil ich heute", "INCOMPLETE"),
    ("Stell den Timer auf", "INCOMPLETE"),
    ("Ich muss noch", "INCOMPLETE"),
    ("Kannst du vielleicht", "INCOMPLETE"),
    ("Kannst du den Branch", "INCOMPLETE"),
    ("Das neue", "INCOMPLETE"),
    ("Und danach", "INCOMPLETE"),
    ("Ähm also", "INCOMPLETE"),
    ("Wie ist das Wetter in", "INCOMPLETE"),
    ("Was steht heute an und", "INCOMPLETE"),
    ("Spiele Musik von", "INCOMPLETE"),
    ("Spiele die Musik des", "INCOMPLETE"),
    ("Spiele Musik also", "INCOMPLETE"),
    ("Erzähl mir etwas über", "INCOMPLETE"),
    ("Ich brauche einen", "INCOMPLETE"),
    ("Ich bin", "INCOMPLETE"),
    ("Ich habe", "INCOMPLETE"),
    ("Guten Morgen", "UNKNOWN"),
    ("Alles klar", "UNKNOWN"),
    ("Okay", "UNKNOWN"),
    ("Wie bitte?", "UNKNOWN"),
    ("Ja", "UNKNOWN"),
    ("Ja, genau", "UNKNOWN"),
    ("Mhm", "UNKNOWN"),
    ("Aha", "UNKNOWN"),
    ("Interessant", "UNKNOWN"),
    ("Kaffee mit Milch", "UNKNOWN"),
    ("Die aktuelle Version", "UNKNOWN"),
    ("Der Branch ist sauber", "UNKNOWN"),
    ("Das klingt gut", "UNKNOWN"),
    ("Du bist nett", "UNKNOWN"),
    ("Der Branch ist online", "UNKNOWN"),
])
def test_german_utterances_use_complete_incomplete_and_unknown_silence_classes(
    monkeypatch, text, expected_state,
):
    import config

    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True)
    monkeypatch.setattr(config, "VOICE_SILENCE_FAST_MS", 450)
    monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 1200)
    monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800)
    segmenter = vad.VadSegmenter(FakeVAD([]), chunk_ms=100)
    segmenter.set_partial_transcript(text, audio_end_sample_index=0)
    segmenter._last_speech_sample_index = 0

    expected_silence_ms = {
        "COMPLETE": 450,
        "INCOMPLETE": 1200,
        "UNKNOWN": 800,
    }[expected_state]

    assert vad.classify_utterance(text).value == expected_state
    assert vad.utterance_is_complete(text) is (expected_state == "COMPLETE")
    assert segmenter._silence_threshold_ms() == expected_silence_ms
