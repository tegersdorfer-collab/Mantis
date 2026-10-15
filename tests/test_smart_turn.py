import asyncio
import time

import numpy as np
import pytest


class _Metadata:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class _FeatureExtractor:
    def __init__(self):
        self.waveforms = []
        self.calls = []

    def __call__(self, audio, *, sampling_rate, return_tensors, **kwargs):
        assert sampling_rate == 16_000
        assert return_tensors == "np"
        self.waveforms.append(np.array(audio, copy=True))
        self.calls.append(kwargs)
        return {"input_features": np.zeros((1, 80, 800), dtype=np.float32)}


class _Session:
    def __init__(self, *, delay_s=0):
        self.delay_s = delay_s
        self.calls = []

    def get_inputs(self):
        return [_Metadata("actual_feature_input", [1, 80, 800])]

    def get_outputs(self):
        return [_Metadata("sentence_probability", [1, 1])]

    def run(self, output_names, input_feed):
        if self.delay_s:
            time.sleep(self.delay_s)
        self.calls.append((output_names, input_feed))
        return [np.array([[0.83]], dtype=np.float32)]


def test_smart_turn_uses_one_eight_second_whisper_window_and_runtime_names(tmp_path):
    from core.smart_turn import SmartTurnAnalyzer

    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    extractor = _FeatureExtractor()
    session = _Session()
    loads = []

    def make_session(path, *, sess_options, providers):
        loads.append((path, sess_options.intra_op_num_threads, providers))
        return session

    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=make_session,
        feature_extractor_factory=lambda: extractor,
    )
    ten_seconds = np.concatenate((
        np.full(2 * 16_000, 3277, dtype=np.int16),
        np.full(8 * 16_000, 16_384, dtype=np.int16),
    ))
    three_seconds = np.full(3 * 16_000, 16_384, dtype=np.int16)

    async def run():
        await analyzer.warmup()
        first = await analyzer.predict(ten_seconds.tobytes())
        second = await analyzer.predict(three_seconds.tobytes())
        await analyzer.close()
        return first, second

    first, second = asyncio.run(run())

    assert first.probability == pytest.approx(0.83)
    assert second.probability == pytest.approx(0.83)
    assert len(loads) == 1
    assert loads[0][1:] == (2, ["CPUExecutionProvider"])
    assert len(extractor.waveforms) == 3
    np.testing.assert_allclose(
        extractor.waveforms[1], np.full(128_000, 0.5, dtype=np.float32), atol=1e-4,
    )
    np.testing.assert_array_equal(extractor.waveforms[2][:80_000], 0.0)
    np.testing.assert_allclose(
        extractor.waveforms[2][80_000:], 0.5, atol=1e-4,
    )
    output_names, inputs = session.calls[1]
    assert output_names == ["sentence_probability"]
    assert list(inputs) == ["actual_feature_input"]
    assert inputs["actual_feature_input"].shape == (1, 80, 800)


def test_smart_turn_features_match_real_whisper_pipecat_reference(tmp_path):
    from transformers import WhisperFeatureExtractor
    from core.smart_turn import SmartTurnAnalyzer, WINDOW_SAMPLES

    class CapturingSession(_Session):
        def run(self, output_names, input_feed):
            self.calls.append((output_names, input_feed))
            return [np.array([[0.83]], dtype=np.float32)]

    long_samples = np.rint(
        np.sin(np.arange(10 * 16_000, dtype=np.float32) / 97.0) * 3276.7,
    ).astype(np.int16)
    short_samples = np.rint(
        np.sin(np.arange(3 * 16_000, dtype=np.float32) / 53.0) * 8192,
    ).astype(np.int16)
    extractor = WhisperFeatureExtractor(chunk_length=8)
    expected_features = []
    for samples in (long_samples, short_samples):
        audio = samples.astype(np.float32) / 32768.0
        reference_window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        reference_window[-min(len(audio), WINDOW_SAMPLES):] = audio[-WINDOW_SAMPLES:]
        expected_features.append(extractor(
            reference_window,
            sampling_rate=16_000,
            return_tensors="np",
            padding="max_length",
            max_length=8 * 16_000,
            truncation=True,
            do_normalize=True,
        )["input_features"])
    session = CapturingSession()
    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=lambda *_args, **_kwargs: session,
        feature_extractor_factory=lambda: extractor,
    )

    async def run():
        results = [
            await analyzer.predict(samples.tobytes())
            for samples in (long_samples, short_samples)
        ]
        await analyzer.close()
        return results

    assert all(
        result.probability == pytest.approx(0.83)
        for result in asyncio.run(run())
    )
    for call, expected in zip(session.calls, expected_features, strict=True):
        actual = call[1]["actual_feature_input"]
        np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)


def test_smart_turn_fake_runtime_does_not_require_onnxruntime(tmp_path, monkeypatch):
    import sys
    from core.smart_turn import SmartTurnAnalyzer

    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=lambda *_args, **_kwargs: _Session(),
        feature_extractor_factory=_FeatureExtractor,
    )

    async def run():
        result = await analyzer.predict(b"\0\0" * 16_000)
        await analyzer.close()
        return result

    assert asyncio.run(run()).probability == pytest.approx(0.83)


def test_smart_turn_warmup_runs_a_dummy_model_inference(tmp_path):
    from core.smart_turn import SmartTurnAnalyzer

    session = _Session()
    extractor = _FeatureExtractor()
    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=lambda *_args, **_kwargs: session,
        feature_extractor_factory=lambda: extractor,
    )

    async def run():
        await analyzer.warmup()
        ready_after_warmup = analyzer.ready
        await analyzer.close()
        return ready_after_warmup

    assert asyncio.run(run()) is True
    assert len(session.calls) == 1
    assert len(extractor.waveforms) == 1
    np.testing.assert_allclose(extractor.waveforms[0], 512 / 32768.0)


def test_smart_turn_preload_scheduler_returns_before_warmup_finishes(monkeypatch):
    import config
    from core import smart_turn

    class SlowAnalyzer:
        def __init__(self):
            self.started = False
            self.finished = False
            self.release = asyncio.Event()

        async def warmup(self):
            self.started = True
            await self.release.wait()
            self.finished = True

    analyzer = SlowAnalyzer()
    monkeypatch.setattr(config, "VOICE_SMART_TURN", True, raising=False)
    monkeypatch.setattr(smart_turn, "get_smart_turn_analyzer", lambda: analyzer)

    async def run():
        task = smart_turn.schedule_smart_turn_preload()
        returned_before_warmup = task is not None and not analyzer.started
        await asyncio.sleep(0)
        started_in_background = analyzer.started and not analyzer.finished
        analyzer.release.set()
        await task
        return returned_before_warmup, started_in_background, analyzer.finished

    assert asyncio.run(run()) == (True, True, True)


def test_smart_turn_prediction_does_not_block_the_event_loop(tmp_path):
    from core.smart_turn import SmartTurnAnalyzer

    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    session = _Session(delay_s=0.04)
    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=lambda *_args, **_kwargs: session,
        feature_extractor_factory=_FeatureExtractor,
    )

    async def run():
        loop_resumed = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.005, loop_resumed.set)
        prediction_task = asyncio.create_task(analyzer.predict(b"\0\0" * 16_000))
        await asyncio.wait_for(loop_resumed.wait(), timeout=0.02)
        prediction = await prediction_task
        await analyzer.close()
        return prediction

    result = asyncio.run(run())

    assert result.probability == pytest.approx(0.83)
    assert result.elapsed_ms >= 30


def test_smart_turn_caches_model_load_failure_for_the_process(tmp_path):
    from core.smart_turn import SmartTurnAnalyzer

    model_path = tmp_path / "smart-turn.onnx"
    model_path.write_bytes(b"fake model")
    attempts = []

    def broken_session_factory(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError("fake load error")

    analyzer = SmartTurnAnalyzer(
        model_path,
        session_factory=broken_session_factory,
        feature_extractor_factory=_FeatureExtractor,
    )

    async def run():
        errors = []
        for _ in range(2):
            try:
                await analyzer.warmup()
            except RuntimeError as exc:
                errors.append(str(exc))
        await analyzer.close()
        return errors

    errors = asyncio.run(run())

    assert attempts == [1]
    assert errors == ["fake load error", "fake load error"]


def test_smart_turn_settings_have_conservative_defaults():
    from settings import MantisSettings

    settings = MantisSettings(_env_file=None)

    assert settings.VOICE_SMART_TURN is True
    assert settings.VOICE_SMART_TURN_TRIGGER_MS == 250
    assert settings.VOICE_SMART_TURN_COMPLETE == 0.6
    assert settings.VOICE_SMART_TURN_INCOMPLETE == 0.3
    assert settings.VOICE_WAKE_CROP_OFFSET_MS == 0
    assert settings.VOICE_RECORD_SEGMENTS is False
    assert settings.VOICE_RECORD_MAX_FILES == 300
    assert settings.VOICE_SMART_TURN_MODEL.endswith(
        "data/turn/smart-turn-v3.2-cpu.onnx",
    )
