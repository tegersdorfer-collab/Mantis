import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import struct
from unittest.mock import MagicMock, patch

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

    def test_discards_segment_shorter_than_min_duration(self):
        # nur 200ms Sprache (< MIN_SEGMENT_MS=300) dann Stille
        probs = [0.9] * 2 + [0.1] * 9
        fake = FakeVAD(probs)
        seg = vad.VadSegmenter(fake, chunk_ms=100)
        events = [seg.process_chunk(silence_chunk()) for _ in range(len(probs))]
        assert all(e is None for e in events)
