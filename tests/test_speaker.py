"""Sprecherverifikation: rein synthetische Audiodaten, keine Aufnahmen."""
from __future__ import annotations

import asyncio
import io
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from core.speaker import (
    SpeakerVerifier,
    decode_wav,
    kaldi_fbank,
    score_embedding,
)


def _wav(samples: np.ndarray, sample_rate: int = 16_000, channels: int = 1) -> bytes:
    pcm = np.asarray(samples, dtype="<i2").tobytes()
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return stream.getvalue()


def _pcm(seconds: float, value: int = 1000) -> bytes:
    return np.full(int(seconds * 16_000), value, dtype="<i2").tobytes()


class FakeStore:
    def __init__(self):
        self.rows: list[dict] = []
        self.minimum_intervals: list[int | None] = []

    def list_embeddings(self):
        return list(self.rows)

    def add_embedding(self, embedding, score, source, **kwargs):
        self.minimum_intervals.append(kwargs.get("minimum_interval_s"))
        self.rows.append({
            "embedding": np.asarray(embedding, dtype=np.float32),
            "score": score,
            "source": source,
            "learned_at": datetime.now(timezone.utc),
        })
        return True

    def clear(self):
        self.rows.clear()

    def history(self, limit=10):
        return self.rows[-limit:][::-1]


def test_kaldi_fbank_returns_cmn_normalized_80_bin_features():
    sample_rate = 16_000
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    audio = 0.2 * np.sin(2 * np.pi * 440 * time)

    features = kaldi_fbank(audio, sample_rate)

    assert features.ndim == 2
    assert features.shape[1] == 80
    assert np.isfinite(features).all()
    assert np.max(np.abs(features.mean(axis=0))) < 1e-4


def test_kaldi_fbank_rejects_wrong_sample_rate():
    with pytest.raises(ValueError, match="16000"):
        kaldi_fbank(np.zeros(16000, dtype=np.float32), 8_000)


def test_score_embedding_uses_the_best_cluster_centroid():
    profile = np.asarray([
        [1.0, 0.0, 0.0],
        [0.99, 0.01, 0.0],
        [0.0, 1.0, 0.0],
        [0.01, 0.99, 0.0],
    ], dtype=np.float32)

    assert score_embedding(np.array([0.0, 1.0, 0.0]), profile) > 0.99
    assert score_embedding(np.array([0.0, 0.0, 1.0]), profile) < 0.01


def test_decode_wav_requires_mono_16khz_pcm():
    samples = np.arange(16_000, dtype=np.int16)

    decoded, sample_rate = decode_wav(_wav(samples))

    assert sample_rate == 16_000
    assert decoded.shape == samples.shape
    assert decoded.dtype == np.float32

    with pytest.raises(ValueError, match="mono"):
        decode_wav(_wav(np.tile(samples, 2), sample_rate=16_000, channels=2))


def test_verifier_returns_no_profile_without_running_embedder():
    called = []
    service = SpeakerVerifier(
        store=FakeStore(),
        embedder=lambda *_args: called.append(True),
    )

    result = asyncio.run(service.verify_pcm(_pcm(1.2)))

    assert result.decision == "no_profile"
    assert result.score is None
    assert called == []


def test_final_segments_shorter_than_half_a_second_can_be_verified():
    store = FakeStore()
    store.rows.append({"embedding": np.array([1.0, 0.0, 0.0])})
    service = SpeakerVerifier(
        store=store,
        embedder=lambda *_args: np.array([1.0, 0.0, 0.0]),
    )

    result = asyncio.run(service.verify_pcm(_pcm(0.3)))

    assert result.decision == "accepted"
    assert result.score == pytest.approx(1.0)


def test_enrollment_requires_twenty_seconds_and_keeps_only_embeddings():
    store = FakeStore()
    service = SpeakerVerifier(
        store=store,
        embedder=lambda _samples, _rate: np.array([1.0, 0.0, 0.0]),
    )

    with pytest.raises(ValueError, match="20 Sekunden"):
        asyncio.run(service.enroll_wavs([_wav(np.full(10 * 16_000, 1000, dtype=np.int16))]))

    result = asyncio.run(service.enroll_wavs([
        _wav(np.full(10 * 16_000, 1000, dtype=np.int16)),
        _wav(np.full(10 * 16_000, 1000, dtype=np.int16)),
    ]))

    assert result["embeddings_added"] == 2
    assert len(store.rows) == 2
    assert all(row["source"] == "enrollment" for row in store.rows)
    assert all(row["embedding"].shape == (3,) for row in store.rows)
    assert all("audio" not in row for row in store.rows)


def test_fake_embedder_accepts_matching_profile_and_rejects_other_speaker():
    store = FakeStore()
    store.rows.append({
        "embedding": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "score": None,
        "source": "enrollment",
        "learned_at": datetime.now(timezone.utc),
    })
    service = SpeakerVerifier(
        store=store,
        embedder=lambda samples, _rate: (
            np.array([1.0, 0.0, 0.0]) if float(samples[0]) > 0
            else np.array([0.0, 0.0, 1.0])
        ),
    )

    accepted, rejected = asyncio.run(_verify_both(service))

    assert accepted.decision == "accepted"
    assert accepted.score >= 0.35
    assert rejected.decision == "rejected"
    assert rejected.score < 0.35


def test_auto_learning_requires_wakeword_confidence_duration_and_non_overlap():
    store = FakeStore()
    service = SpeakerVerifier(
        store=store,
        embedder=lambda *_args: (_ for _ in ()).throw(
            AssertionError("übergebenes Embedding soll wiederverwendet werden"),
        ),
    )
    embedding = np.array([1.0, 0.0, 0.0])

    learned = asyncio.run(service.maybe_learn(
        _pcm(1.6), 0.7,
        acoustic_wakeword=True,
        bargein=False,
        overlapping=False,
        embedding=embedding,
    ))
    rejected = asyncio.run(service.maybe_learn(
        _pcm(1.6), 0.7,
        acoustic_wakeword=False,
        bargein=False,
        overlapping=False,
        embedding=embedding,
    ))
    short = asyncio.run(service.maybe_learn(
        _pcm(1.2), 0.7,
        acoustic_wakeword=True,
        bargein=False,
        overlapping=False,
        embedding=embedding,
    ))
    overlapped = asyncio.run(service.maybe_learn(
        _pcm(1.6), 0.7,
        acoustic_wakeword=True,
        bargein=True,
        overlapping=True,
        embedding=embedding,
    ))

    assert learned is True
    assert rejected is short is overlapped is False
    assert store.minimum_intervals == [600]
    assert len(store.rows) == 1


def test_onnx_runtime_uses_session_discovered_names_and_single_thread_options(tmp_path):
    model_path = tmp_path / "fake-speaker.onnx"
    model_path.write_bytes(b"placeholder")
    captured = {}

    class FakeSession:
        def get_inputs(self):
            return [type("Input", (), {"name": "audio_features"})()]

        def get_outputs(self):
            return [type("Output", (), {"name": "speaker_vector"})()]

        def run(self, outputs, feeds):
            captured["outputs"] = outputs
            captured["feeds"] = feeds
            return [np.ones((1, 192), dtype=np.float32)]

    def session_factory(path, *, sess_options, providers):
        captured["path"] = path
        captured["threads"] = (sess_options.intra_op_num_threads, sess_options.inter_op_num_threads)
        captured["providers"] = providers
        return FakeSession()

    service = SpeakerVerifier(model_path, session_factory=session_factory)
    actual = asyncio.run(service.embed_waveform(np.ones(16_000, dtype=np.float32)))
    asyncio.run(service.close())

    assert actual.shape == (192,)
    assert captured["outputs"] == ["speaker_vector"]
    assert captured["feeds"]["audio_features"].shape[0] == 1
    assert captured["threads"] == (1, 1)
    assert captured["providers"] == ["CPUExecutionProvider"]


async def _verify_both(service):
    return await service.verify_pcm(_pcm(1.2, 1000)), await service.verify_pcm(_pcm(1.2, -1000))


def test_reference_embedding_matches_sherpa_onnx_fixture_when_model_exists():
    from core.speaker import DEFAULT_MODEL_PATH

    model_path = Path(__import__("os").environ.get("VOICE_SPEAKER_MODEL", DEFAULT_MODEL_PATH))
    if not model_path.is_file():
        pytest.skip("Sprecher-ONNX-Modell fehlt")
    fixture_path = Path(__file__).parent / "fixtures" / "speaker_reference_synthetic.npz"
    with np.load(fixture_path) as fixture:
        audio = fixture["audio"]
        expected = fixture["embedding"]
        sample_rate = int(fixture["sample_rate"])

    service = SpeakerVerifier(model_path=model_path)
    actual = asyncio.run(service.embed_waveform(audio, sample_rate))
    cosine = float(actual @ expected / (np.linalg.norm(actual) * np.linalg.norm(expected)))
    asyncio.run(service.close())

    assert cosine >= 0.99
