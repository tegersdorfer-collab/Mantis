import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
from unittest.mock import MagicMock, patch

import core.wakeword as wakeword


def make_detector_with_scores(tmp_path, scores, embeddings_shape=(16, wakeword.EMBEDDING_DIM)):
    """Baut einen WakeWordDetector, dessen Klassifikator-Session bei aufeinanderfolgenden
    check()-Aufrufen die gegebene scores-Sequenz zurückgibt (eine pro Aufruf)."""
    model_path = tmp_path / "mantis.onnx"
    model_path.write_bytes(b"fake")
    with patch.object(wakeword, "_load_audio_features") as mock_af, \
         patch.object(wakeword, "_load_session") as mock_session_loader:
        mock_features = MagicMock()
        mock_features._get_embeddings.return_value = np.zeros(embeddings_shape, dtype=np.float32)
        mock_af.return_value = mock_features

        mock_session = MagicMock()
        mock_session.get_inputs.return_value = [MagicMock(name="x")]
        mock_session.get_inputs.return_value[0].name = "x"
        mock_session.run.side_effect = [[np.array([[s]], dtype=np.float32)] for s in scores]
        mock_session_loader.return_value = mock_session

        detector = wakeword.WakeWordDetector(model_path, threshold=0.5)
    return detector


class TestWakeWordDetector:
    def test_check_false_on_single_hit_below_consecutive_requirement(self, tmp_path):
        # Ein einzelner über-Schwelle-Score reicht nicht - Hysterese erfordert
        # CONSECUTIVE_HITS_REQUIRED aufeinanderfolgende Treffer (siehe Live-Streaming-
        # Test in docs/superpowers/plans/2026-07-05-vad-wakeword-streaming.md Task 6:
        # isolierte Einzel-Chunks über der Schwelle waren an Puffer-Übergängen nicht
        # zuverlässig genug).
        detector = make_detector_with_scores(tmp_path, scores=[0.8])
        assert detector.check(b"\x00\x00" * 1280) is False

    def test_check_true_after_enough_consecutive_hits(self, tmp_path):
        detector = make_detector_with_scores(tmp_path, scores=[0.8] * wakeword.CONSECUTIVE_HITS_REQUIRED)
        results = [detector.check(b"\x00\x00" * 1280) for _ in range(wakeword.CONSECUTIVE_HITS_REQUIRED)]
        assert results == [False] * (wakeword.CONSECUTIVE_HITS_REQUIRED - 1) + [True]

    def test_debounced_hit_exposes_end_of_first_positive_frame(self, tmp_path):
        detector = make_detector_with_scores(
            tmp_path, scores=[0.8] * wakeword.CONSECUTIVE_HITS_REQUIRED,
        )

        results = [
            detector.check(b"\x00\x00" * wakeword.WAKEWORD_REQUIRED_SAMPLES)
            for _ in range(wakeword.CONSECUTIVE_HITS_REQUIRED)
        ]

        assert results[-1] is True
        assert detector.first_positive_frame_end_sample == wakeword.WAKEWORD_REQUIRED_SAMPLES

    def test_check_resets_streak_on_a_low_score(self, tmp_path):
        # Trifft mehrfach über der Schwelle, dann einmal darunter, dann wieder mehrfach
        # darüber - der Streak muss beim Unterschreiten zurückgesetzt werden, nicht
        # einfach weiterzählen.
        scores = [0.8] * (wakeword.CONSECUTIVE_HITS_REQUIRED - 1) + [0.1] + [0.8] * wakeword.CONSECUTIVE_HITS_REQUIRED
        detector = make_detector_with_scores(tmp_path, scores=scores)
        results = [detector.check(b"\x00\x00" * 1280) for _ in scores]
        assert results[wakeword.CONSECUTIVE_HITS_REQUIRED - 1] is False  # der Score-Einbruch selbst
        assert all(r is False for r in results[:wakeword.CONSECUTIVE_HITS_REQUIRED])
        assert results[-1] is True  # erst nach erneuter voller Streak wieder True

    def test_check_false_when_score_below_threshold(self, tmp_path):
        detector = make_detector_with_scores(tmp_path, scores=[0.2] * wakeword.CONSECUTIVE_HITS_REQUIRED)
        results = [detector.check(b"\x00\x00" * 1280) for _ in range(wakeword.CONSECUTIVE_HITS_REQUIRED)]
        assert all(r is False for r in results)

    def test_check_rejects_wrong_frame_size(self, tmp_path):
        detector = make_detector_with_scores(tmp_path, scores=[0.0])
        try:
            detector.check(b"\x00\x00" * 128)
            assert False, "expected ValueError for wrong frame size"
        except ValueError:
            pass

    def test_check_false_when_not_enough_buffered_context_for_one_window(self, tmp_path):
        detector = make_detector_with_scores(
            tmp_path, scores=[0.9] * wakeword.CONSECUTIVE_HITS_REQUIRED,
            embeddings_shape=(5, wakeword.EMBEDDING_DIM),
        )
        assert detector.check(b"\x00\x00" * 1280) is False

    def test_check_uses_rolling_buffer_across_calls(self, tmp_path):
        detector = make_detector_with_scores(tmp_path, scores=[0.1, 0.1])
        detector.check(b"\x00\x00" * 1280)
        detector.check(b"\x00\x00" * 1280)

        audio_features_mock = detector._audio_features
        first_buffer = audio_features_mock._get_embeddings.call_args_list[0].args[0]
        second_buffer = audio_features_mock._get_embeddings.call_args_list[1].args[0]
        assert first_buffer.shape == (wakeword.BUFFER_SAMPLES,)
        assert second_buffer.shape == (wakeword.BUFFER_SAMPLES,)
