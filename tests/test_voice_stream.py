import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.voice_stream import VoiceStreamSession, VAD_FRAME_BYTES, WAKEWORD_FRAME_BYTES
from core.vad import SegmentEvent


@pytest.fixture(autouse=True)
def _isolate_process_wide_stt_runtime(monkeypatch):
    import config
    from core import stt_runtime

    monkeypatch.setattr(config, "VOICE_SMART_TURN", False, raising=False)
    monkeypatch.setattr(
        stt_runtime, "_samples", deque(maxlen=stt_runtime._SAMPLE_WINDOW),
    )


def make_session(segment_events, wake_hits, conversation_active=False):
    fake_segmenter = MagicMock()
    fake_segmenter.process_chunk.side_effect = segment_events
    fake_wakeword = MagicMock()
    fake_wakeword.check.side_effect = wake_hits
    return VoiceStreamSession(
        vad_segmenter=fake_segmenter,
        wakeword_detector=fake_wakeword,
        conversation_active_fn=lambda: conversation_active,
    )


def vad_frame(byte_value: int = 0) -> bytes:
    """A single full-size VAD frame (exactly VAD_FRAME_BYTES bytes)."""
    return bytes([byte_value]) * VAD_FRAME_BYTES


def wakeword_frame(byte_value: int = 0) -> bytes:
    """A single full-size wake-word frame (exactly WAKEWORD_FRAME_BYTES bytes)."""
    return bytes([byte_value]) * WAKEWORD_FRAME_BYTES


class _FakeTurnAnalyzer:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.audios = []

    async def predict(self, audio):
        from core.smart_turn import SmartTurnPrediction

        self.audios.append(audio)
        return SmartTurnPrediction(next(self.probabilities), 3.5)


class _FailingTurnAnalyzer:
    def __init__(self):
        self.calls = 0

    async def predict(self, _audio):
        self.calls += 1
        raise RuntimeError("fake model failure")


def _smart_turn_session(analyzer, monkeypatch, *, transcript="mach das Licht an"):
    import config
    import core.voice_stream as voice_stream
    from core.vad import VadSegmenter

    class FakeVAD:
        def speech_probability(self, frame):
            return 1.0 if frame != b"\0" * len(frame) else 0.0

    monkeypatch.setattr(config, "VOICE_SMART_TURN", True, raising=False)
    monkeypatch.setattr(config, "VOICE_SMART_TURN_TRIGGER_MS", 250, raising=False)
    monkeypatch.setattr(config, "VOICE_SMART_TURN_COMPLETE", 0.6, raising=False)
    monkeypatch.setattr(config, "VOICE_SMART_TURN_INCOMPLETE", 0.3, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_FAST_MS", 450, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_SLOW_MS", 2000, raising=False)
    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", True, raising=False)
    monkeypatch.setattr(
        voice_stream, "transcribe_pcm", AsyncMock(return_value=(transcript, False)),
    )
    segmenter = VadSegmenter(FakeVAD(), chunk_ms=32)
    session = VoiceStreamSession(
        vad_segmenter=segmenter,
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_interval_ms=0,
        partial_executor_available=lambda: False,
        smart_turn_analyzer=analyzer,
    )
    return session, segmenter


@pytest.mark.parametrize(
    ("probabilities", "expected_decision", "max_silence_ms", "min_checks"),
    [
        ([0.9], "model_complete", 400, 1),
        ([0.6], "model_complete", 400, 1),
        ([0.1] * 8, "model_incomplete", 2048, 5),
    ],
)
def test_smart_turn_scores_choose_fast_end_or_recheck_until_slow_limit(
    monkeypatch, probabilities, expected_decision, max_silence_ms, min_checks,
):
    analyzer = _FakeTurnAnalyzer(probabilities)
    session, _segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
            await asyncio.sleep(0)
        result = None
        for _ in range(70):
            result = await session.handle_chunk(silence, muted=False)
            await asyncio.sleep(0)
            if result is not None:
                break
        await session.close()
        return result

    result = asyncio.run(run())

    assert result is not None
    assert result["turn_decision"] == expected_decision
    assert result["silence_used_ms"] <= max_silence_ms
    assert len(analyzer.audios) >= min_checks
    assert result["turn_model_p"] == pytest.approx(probabilities[len(analyzer.audios) - 1])
    assert result["turn_model_ms"] >= 3.5


def test_middle_smart_turn_score_keeps_the_partial_text_heuristic(monkeypatch):
    analyzer = _FakeTurnAnalyzer([0.45])
    session, segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
            await asyncio.sleep(0)
        segmenter.set_partial_transcript("mach das Licht an")
        result = None
        for _ in range(20):
            result = await session.handle_chunk(silence, muted=False)
            await asyncio.sleep(0)
            if result is not None:
                break
        await session.close()
        return result

    result = asyncio.run(run())

    assert result is not None
    assert result["turn_decision"] == "heuristic"
    assert result["silence_used_ms"] <= 500
    assert len(analyzer.audios) == 1


def test_speech_progress_callback_receives_live_audio_and_segment_generation():
    segmenter = MagicMock()
    segmenter.is_speaking = True
    segmenter.current_chunk_is_speech = True
    segmenter.current_speech_ms = 640
    segmenter.current_audio = b"\x01\x00" * 8_000
    segmenter.process_chunk.return_value = None
    callbacks = []
    session = VoiceStreamSession(
        vad_segmenter=segmenter,
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_interval_ms=0,
        partial_executor_available=lambda: False,
        bargein_active_fn=lambda: True,
        on_speech_progress=lambda audio, speech_ms, generation: callbacks.append(
            (audio, speech_ms, generation),
        ),
    )

    async def run():
        await session.handle_chunk(vad_frame(), muted=False)
        await session.close()

    asyncio.run(run())

    assert callbacks == [(segmenter.current_audio, 640, 0)]


def test_exact_incomplete_threshold_uses_heuristic_fallback(monkeypatch):
    analyzer = _FakeTurnAnalyzer([0.3])
    session, _segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
        result = None
        for _ in range(40):
            result = await session.handle_chunk(silence, muted=False)
            await asyncio.sleep(0)
            if result is not None:
                break
        await session.close()
        return result

    result = asyncio.run(run())

    # The incomplete branch is strict (< 0.3), so p == 0.3 stays on the heuristic.
    assert result is not None
    assert result["turn_decision"] == "heuristic"
    assert result["silence_used_ms"] == 800
    assert len(analyzer.audios) == 1


def test_smart_turn_error_falls_back_to_the_previous_silence_logic(monkeypatch, caplog):
    import config

    monkeypatch.setattr(config, "VOICE_SMART_TURN_MODEL", "test-smart-turn-fallback.onnx", raising=False)
    analyzer = _FailingTurnAnalyzer()
    session, segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        results = []
        for _turn in range(2):
            for _ in range(12):
                await session.handle_chunk(speech, muted=False)
                await asyncio.sleep(0)
            segmenter.set_partial_transcript("mach das Licht an")
            result = None
            for _ in range(20):
                result = await session.handle_chunk(silence, muted=False)
                await asyncio.sleep(0)
                if result is not None:
                    break
            results.append(result)
        await session.close()
        return results

    results = asyncio.run(run())

    assert all(result is not None for result in results)
    assert all(result["turn_decision"] == "fallback" for result in results)
    assert all(result["turn_model_p"] is None for result in results)
    assert all(result["silence_used_ms"] <= 500 for result in results)
    assert analyzer.calls == 1
    assert caplog.text.count("Smart Turn nicht verfügbar") == 1


def test_smart_turn_model_is_warmed_in_background_when_speech_starts(monkeypatch):
    class SlowWarmupAnalyzer:
        def __init__(self):
            self.started = False
            self.finished = False
            self.release = asyncio.Event()

        async def warmup(self):
            self.started = True
            await self.release.wait()
            self.finished = True

    analyzer = SlowWarmupAnalyzer()
    session, _segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512

    async def run():
        await session.handle_chunk(speech, muted=False)
        await asyncio.sleep(0)
        started_without_waiting = analyzer.started and not analyzer.finished
        analyzer.release.set()
        await asyncio.sleep(0)
        await session.close()
        return started_without_waiting

    assert asyncio.run(run()) is True


def test_unready_smart_turn_uses_heuristic_without_waiting_for_prediction(monkeypatch):
    import config
    import core.voice_stream as voice_stream
    from core.vad import VadSegmenter

    class FakeVAD:
        def speech_probability(self, frame):
            return 1.0 if frame != b"\0" * len(frame) else 0.0

    class ColdAnalyzer:
        ready = False

        def __init__(self):
            self.predictions = 0
            self.release = asyncio.Event()

        async def warmup(self):
            await self.release.wait()

        async def predict(self, _audio):
            self.predictions += 1
            raise AssertionError("Vor Bereitschaft darf predict nicht starten")

    monkeypatch.setattr(config, "VOICE_SMART_TURN", True, raising=False)
    monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
    monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", False, raising=False)
    monkeypatch.setattr(
        voice_stream, "transcribe_pcm",
        AsyncMock(return_value=("Mach das Licht an", False)),
    )
    analyzer = ColdAnalyzer()
    session = VoiceStreamSession(
        vad_segmenter=VadSegmenter(FakeVAD(), chunk_ms=32),
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_interval_ms=0,
        partial_executor_available=lambda: False,
        smart_turn_analyzer=analyzer,
    )
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
        result = None
        for _ in range(30):
            result = await session.handle_chunk(silence, muted=False)
            if result is not None:
                break
            await asyncio.sleep(0)
        await session.close()
        return result

    result = asyncio.run(run())

    assert result is not None
    assert result["turn_decision"] == "heuristic"
    assert analyzer.predictions == 0


def test_smart_turn_can_start_after_warmup_finishes_mid_segment(monkeypatch):
    from core.smart_turn import SmartTurnPrediction

    class WarmingAnalyzer:
        def __init__(self):
            self.ready = False
            self.release = asyncio.Event()
            self.predictions = 0

        async def warmup(self):
            await self.release.wait()
            self.ready = True

        async def predict(self, _audio):
            self.predictions += 1
            return SmartTurnPrediction(0.9, 1.0)

    analyzer = WarmingAnalyzer()
    session, _segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
        for _ in range(10):
            await session.handle_chunk(silence, muted=False)
            await asyncio.sleep(0)
        assert analyzer.predictions == 0
        analyzer.release.set()
        await asyncio.sleep(0)
        await session.handle_chunk(silence, muted=False)
        await asyncio.sleep(0)
        result = await session.handle_chunk(silence, muted=False)
        await session.close()
        return result

    result = asyncio.run(run())

    assert result is not None
    assert result["turn_decision"] == "model_complete"
    assert analyzer.predictions == 1


def test_resumed_speech_cancels_a_running_smart_turn_check(monkeypatch):
    from core.smart_turn import SmartTurnPrediction
    from core.vad import VadSegmenter

    class FakeVAD:
        def speech_probability(self, frame):
            return 1.0 if frame != b"\0" * len(frame) else 0.0

    class BlockingAnalyzer:
        ready = True

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def predict(self, _audio):
            self.started.set()
            await self.release.wait()
            return SmartTurnPrediction(0.9, 1.0)

    analyzer = BlockingAnalyzer()
    segmenter = VadSegmenter(FakeVAD(), chunk_ms=32)
    session, _ = _smart_turn_session(analyzer, monkeypatch)
    session._segmenter = segmenter
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
        for _ in range(20):
            await session.handle_chunk(silence, muted=False)
            if analyzer.started.is_set():
                break
            await asyncio.sleep(0)
        assert analyzer.started.is_set()
        resumed_result = await session.handle_chunk(speech, muted=False)
        analyzer.release.set()
        await asyncio.sleep(0)
        silence_result = await session.handle_chunk(silence, muted=False)
        await session.close()
        return resumed_result, silence_result, segmenter.is_speaking

    resumed_result, silence_result, is_speaking = asyncio.run(run())

    assert resumed_result is None
    assert silence_result is None
    assert is_speaking is True


def test_close_cancels_a_running_smart_turn_check_before_waiting_for_partial_cleanup(
    monkeypatch,
):
    from core.smart_turn import SmartTurnPrediction

    class BlockingAnalyzer:
        ready = True

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def predict(self, _audio):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return SmartTurnPrediction(0.9, 1.0)

    analyzer = BlockingAnalyzer()
    session, _segmenter = _smart_turn_session(analyzer, monkeypatch)
    speech = b"\x10\x00" * 512
    silence = b"\0\0" * 512
    partial_cancelled = asyncio.Event()
    release_partial_cleanup = asyncio.Event()

    async def slow_partial_cleanup():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            partial_cancelled.set()
            await release_partial_cleanup.wait()
            raise

    async def run():
        for _ in range(12):
            await session.handle_chunk(speech, muted=False)
        for _ in range(20):
            await session.handle_chunk(silence, muted=False)
            if analyzer.started.is_set():
                break
            await asyncio.sleep(0)
        assert analyzer.started.is_set()
        smart_turn_task = session._smart_turn_task
        session._partial_task = asyncio.create_task(slow_partial_cleanup())
        await asyncio.sleep(0)
        closing = asyncio.create_task(session.close())
        try:
            await partial_cancelled.wait()
            await asyncio.sleep(0)
            assert analyzer.cancelled.is_set()
            assert smart_turn_task.cancelled()
        finally:
            release_partial_cleanup.set()
            await closing

    asyncio.run(run())


def test_debounced_wakeword_crop_uses_first_positive_frame_end(monkeypatch):
    import struct
    import config
    import core.voice_stream as voice_stream
    from core.vad import SegmentEvent

    class DebouncedWakeword:
        def __init__(self):
            self.calls = 0
            self.first_positive_frame_end_sample = None
            self.hit_frame_end_sample = None

        def check(self, _frame):
            self.calls += 1
            if self.calls == 10:
                self.first_positive_frame_end_sample = 12_800  # 800 ms
            if self.calls == 12:
                self.hit_frame_end_sample = 15_360  # 960 ms after debounce
                return True
            return False

    class Segmenter:
        is_speaking = False
        current_chunk_is_speech = False
        current_silence_ms = 800

        def __init__(self):
            self.audio = bytearray()

        def process_chunk(self, frame):
            self.audio.extend(frame)
            if len(self.audio) == 60 * 512 * 2:
                return SegmentEvent(
                    audio=bytes(self.audio), duration_ms=2720,
                    last_speech_sample_index=20_000, silence_ms=800,
                )
            return None

    audio = struct.pack("<30720h", *range(30_720))
    wakeword = DebouncedWakeword()
    captured = []

    async def transcribe(pcm):
        captured.append(pcm)
        return "Mach das Licht an", False

    session = VoiceStreamSession(
        vad_segmenter=Segmenter(),
        wakeword_detector=wakeword,
        conversation_active_fn=lambda: False,
        partial_interval_ms=0,
        partial_executor_available=lambda: False,
    )
    monkeypatch.setattr(config, "VOICE_WAKE_CROP_OFFSET_MS", 0, raising=False)
    monkeypatch.setattr(voice_stream, "transcribe_pcm", transcribe)

    result = asyncio.run(session.handle_chunk(audio, muted=False))

    assert wakeword.first_positive_frame_end_sample == 12_800
    assert wakeword.hit_frame_end_sample == 15_360
    assert result is not None and result["wakeword_triggered"] is True
    assert result["wakeword_position_ms"] == 800
    assert struct.unpack("<h", captured[0][:2])[0] == 10_400


def test_wakeword_audio_crop_keeps_a_conservative_preroll():
    from core.voice_stream import crop_wakeword_audio

    samples = list(range(5000))
    audio = __import__("struct").pack("<5000h", *samples)

    cropped, absolute_start = crop_wakeword_audio(
        audio, segment_start_sample=500, wakeword_hit_sample=3000,
    )
    unchanged, unchanged_start = crop_wakeword_audio(
        audio, segment_start_sample=500, wakeword_hit_sample=None,
    )

    assert absolute_start == 600
    assert __import__("struct").unpack("<h", cropped[:2])[0] == 100
    assert unchanged == audio
    assert unchanged_start == 500


@pytest.mark.parametrize(
    ("transcriber_name", "wrapper_name"),
    [
        ("transcribe_audio_with_hint", "transcribe_pcm"),
        ("transcribe_audio_partial_with_hint", "transcribe_pcm_partial"),
    ],
)
def test_stream_stt_keeps_raw_mantis_like_words_until_acoustic_confirmation(
    monkeypatch, transcriber_name, wrapper_name,
):
    import core.voice as voice
    import core.voice_stream as voice_stream

    transcriber = AsyncMock(return_value=("Mentis, bitte", False))
    monkeypatch.setattr(voice, transcriber_name, transcriber)
    wrapper = getattr(voice_stream, wrapper_name)

    asyncio.run(wrapper(b"\0\0" * 512))

    assert transcriber.await_args.kwargs["wakeword_triggered"] is False


@pytest.mark.parametrize("wakeword_hit", [True, False])
def test_partial_transcription_uses_wakeword_crop_and_safe_prefix(
    monkeypatch, wakeword_hit,
):
    import struct
    import config

    segment_audio = struct.pack("<8000h", *range(8000))
    partial_audio = []

    class Clock:
        now = 1.0

        def __call__(self):
            return self.now

    class Segmenter:
        is_speaking = False
        current_chunk_is_speech = False
        current_audio = segment_audio

        def process_chunk(self, _frame):
            self.is_speaking = True
            self.current_chunk_is_speech = True
            return None

        def set_partial_transcript(self, text, audio_end_sample_index=None):
            self.partial_text = text
            self.partial_audio_end_sample_index = audio_end_sample_index

    async def partial_stt(audio):
        partial_audio.append(audio)
        return "Mentis, bitte mach das Licht an", False

    clock = Clock()
    segmenter = Segmenter()
    session = VoiceStreamSession(
        vad_segmenter=segmenter,
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_transcriber=partial_stt,
        partial_executor_available=lambda: True,
        partial_interval_ms=1,
        clock=clock,
    )
    session._vad_samples_processed = 10_000
    if wakeword_hit:
        session._wakeword_hits.append(7000)
    monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 2000, raising=False)

    async def run():
        await session.handle_chunk(vad_frame(), muted=False)
        clock.now += 0.01
        await session.handle_chunk(vad_frame(), muted=False)
        await session._partial_task
        result = (session._partial_text, segmenter.partial_text)
        await session.close()
        return result

    partial_text, heuristic_text = asyncio.run(run())

    if wakeword_hit:
        assert partial_audio == [segment_audio[3152:]]
        assert partial_text == "Mantis, bitte mach das Licht an"
        assert heuristic_text == "Mentis, bitte mach das Licht an"
    else:
        assert partial_audio == [segment_audio]
        assert partial_text == "Mentis, bitte mach das Licht an"
        assert heuristic_text == "Mentis, bitte mach das Licht an"


def test_enabled_segment_recording_saves_final_transcript_and_turn_metadata(monkeypatch):
    import config
    import core.voice_stream as voice_stream

    audio = b"\x01\x00" * 8000
    recordings = []

    class Segmenter:
        is_speaking = False
        current_chunk_is_speech = False

        def process_chunk(self, _frame):
            return SegmentEvent(
                audio=audio, duration_ms=1800, last_speech_sample_index=15_000,
                silence_ms=800,
            )

    async def final_stt(_audio):
        return "Mach das Licht an", False

    def record(audio_bytes, metadata, **kwargs):
        recordings.append((audio_bytes, metadata, kwargs))

    monkeypatch.setattr(config, "VOICE_RECORD_SEGMENTS", True, raising=False)
    monkeypatch.setattr(voice_stream, "transcribe_pcm", final_stt)
    monkeypatch.setattr(voice_stream, "record_voice_segment", record, raising=False)
    session = VoiceStreamSession(
        vad_segmenter=Segmenter(),
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_executor_available=lambda: False,
    )

    result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))

    assert result["text"] == "Mach das Licht an"
    assert len(recordings) == 1
    recorded_audio, metadata, kwargs = recordings[0]
    assert recorded_audio == audio
    assert metadata["transcript"] == "Mach das Licht an"
    assert metadata["partial_transcripts"] == []
    assert metadata["silence_used_ms"] == 800
    assert metadata["turn_model_p"] is None
    assert metadata["heuristic_class"] == "UNKNOWN"
    assert metadata["wakeword_position_ms"] is None
    assert kwargs == {}


def test_enabled_recording_keeps_final_segments_dropped_as_unaddressed(monkeypatch):
    import config
    import core.voice_stream as voice_stream

    audio = b"\x02\x00" * 8000
    recordings = []

    class Segmenter:
        is_speaking = False
        current_chunk_is_speech = False

        def process_chunk(self, _frame):
            return SegmentEvent(audio=audio, duration_ms=1800, silence_ms=800)

    monkeypatch.setattr(config, "VOICE_RECORD_SEGMENTS", True, raising=False)
    monkeypatch.setattr(
        voice_stream, "record_voice_segment",
        lambda audio_bytes, metadata, **_kwargs: recordings.append((audio_bytes, metadata)),
        raising=False,
    )
    session = VoiceStreamSession(
        vad_segmenter=Segmenter(),
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: False,
        partial_executor_available=lambda: False,
    )

    result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))

    assert result is None
    assert len(recordings) == 1
    assert recordings[0][0] == audio
    assert recordings[0][1]["transcript"] is None


def test_partial_transcript_history_keeps_segment_relative_offset_and_wakeword_prefix():
    class Segmenter:
        is_speaking = True

        def set_partial_transcript(self, _text, audio_end_sample_index=None):
            self.audio_end_sample_index = audio_end_sample_index

    async def partial_stt(_audio):
        return "Mentis, bitte", False

    segmenter = Segmenter()
    session = VoiceStreamSession(
        vad_segmenter=segmenter,
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: True,
        partial_transcriber=partial_stt,
        partial_executor_available=lambda: True,
    )

    result = asyncio.run(
        session._run_partial(
            b"\0\0" * 8000, generation=0,
            audio_end_sample_index=64_000, wakeword_triggered=True,
            segment_offset_ms=500.0,
        ),
    )

    assert result == "Mantis, bitte"
    assert session._partial_transcripts == [
        {"offset_ms": 500.0, "text": "Mantis, bitte"},
    ]
    assert segmenter.audio_end_sample_index == 64_000


@pytest.mark.parametrize("wakeword_hit", [True, False])
def test_final_transcription_uses_wakeword_crop_and_only_prefixes_with_acoustic_hit(
    monkeypatch, wakeword_hit,
):
    import struct
    import core.voice_stream as voice_stream

    segment_audio = struct.pack("<8000h", *range(8000))

    class Segmenter:
        is_speaking = False
        current_chunk_is_speech = False
        current_silence_ms = 800

        def process_chunk(self, _frame):
            return SegmentEvent(
                audio=segment_audio,
                duration_ms=1800,
                last_speech_sample_index=15_000,
                silence_ms=800,
            )

    transcribed_audio = []

    async def final_stt(audio):
        transcribed_audio.append(audio)
        return "Meintest, wie spät ist es?", False

    session = VoiceStreamSession(
        vad_segmenter=Segmenter(),
        wakeword_detector=MagicMock(),
        conversation_active_fn=lambda: not wakeword_hit,
        partial_executor_available=lambda: False,
    )
    session._vad_samples_processed = 8000
    if wakeword_hit:
        session._wakeword_hits.append(5000)
    monkeypatch.setattr(voice_stream, "transcribe_pcm", final_stt)

    result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))

    if wakeword_hit:
        assert transcribed_audio == [segment_audio[4176:]]
        assert result["text"] == "Mantis, wie spät ist es?"
    else:
        assert transcribed_audio == [segment_audio]
        assert result["text"] == "Meintest, wie spät ist es?"




def test_voice_bargein_settings_default_and_validate():
    from settings import MantisSettings

    settings = MantisSettings(_env_file=None)
    assert settings.VOICE_BARGEIN is True
    assert settings.VOICE_BARGEIN_MIN_SPEECH_MS == 300
    assert settings.VOICE_BARGEIN_MIN_RMS == 800


class TestVoiceStreamSession:
    def test_ignores_muted_chunks(self):
        session = make_session(segment_events=[], wake_hits=[])
        result = asyncio.run(session.handle_chunk(b"\x00\x00", muted=True))
        assert result is None

    def test_segment_reports_why_live_partial_stt_was_skipped(self, monkeypatch):
        import config
        import core.voice_stream as voice_stream

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""
            frames = 0

            def process_chunk(self, frame):
                self.frames += 1
                self.current_audio += frame
                if self.frames == 1:
                    self.current_chunk_is_speech = False
                    return None
                self.is_speaking = True
                self.current_chunk_is_speech = True
                if self.frames == 4:
                    self.is_speaking = False
                    self.current_chunk_is_speech = False
                    return SegmentEvent(
                        audio=self.current_audio,
                        duration_ms=900,
                        last_speech_sample_index=len(self.current_audio) // 2,
                    )
                return None

        clock = FakeClock()
        session = VoiceStreamSession(
            vad_segmenter=SpeechSegmenter(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_transcriber=AsyncMock(return_value="Teiltext"),
            partial_executor_available=lambda: True,
            partial_interval_ms=700,
            clock=clock,
        )
        monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
        monkeypatch.setattr(
            voice_stream.stt_runtime, "estimate_stt_runtime_ms", lambda _seconds: 1200,
        )
        monkeypatch.setattr(
            voice_stream, "transcribe_pcm", AsyncMock(return_value=("Final", False)),
        )

        async def run():
            await session.handle_chunk(vad_frame(), muted=False)
            await session.handle_chunk(vad_frame(), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(), muted=False)
            return await session.handle_chunk(vad_frame(), muted=False)

        result = asyncio.run(run())

        assert result["partial_runs"] == 0
        assert result["partial_skip_reasons"]["budget"] == 1
        assert result["partial_skip_reasons"]["no_speech"] == 1
        assert set(result["partial_skip_reasons"]) == {
            "busy", "budget", "interval", "no_speech", "disabled", "not_wired",
        }

    @pytest.mark.parametrize(
        ("reason", "interval_ms", "transcriber", "executor_available", "advance"),
        [
            ("disabled", 0, True, True, False),
            ("not_wired", 32, False, True, False),
            ("interval", 1000, True, True, False),
            ("busy", 32, True, False, True),
        ],
    )
    def test_partial_skip_reasons_count_live_skip_causes(
        self, monkeypatch, reason, interval_ms, transcriber,
        executor_available, advance,
    ):
        import core.voice_stream as voice_stream

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""
            frames = 0

            def process_chunk(self, frame):
                self.frames += 1
                self.current_audio += frame
                if self.frames == 1:
                    self.current_chunk_is_speech = False
                    return None
                self.is_speaking = True
                self.current_chunk_is_speech = True
                if self.frames == 4:
                    self.is_speaking = False
                    self.current_chunk_is_speech = False
                    return SegmentEvent(
                        audio=self.current_audio,
                        duration_ms=96,
                        last_speech_sample_index=len(self.current_audio) // 2,
                    )
                return None

        clock = FakeClock()
        session = VoiceStreamSession(
            vad_segmenter=SpeechSegmenter(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_transcriber=(AsyncMock() if transcriber else None),
            partial_executor_available=lambda: executor_available,
            partial_interval_ms=interval_ms,
            clock=clock,
        )
        monkeypatch.setattr(
            voice_stream, "transcribe_pcm", AsyncMock(return_value=("Final", False)),
        )

        async def run():
            result = None
            for frame in range(4):
                if advance and frame == 2:
                    clock.advance(0.1)
                result = await session.handle_chunk(vad_frame(), muted=False)
            return result

        result = asyncio.run(run())

        assert result["partial_skip_reasons"][reason] > 0
        assert set(result["partial_skip_reasons"]) == {
            "busy", "budget", "interval", "no_speech", "disabled", "not_wired",
        }

    def test_bargein_requires_sustained_speech_and_keeps_segment_for_final_stt(
        self, monkeypatch,
    ):
        import core.voice_stream as vs
        import config

        class FakeClock:
            now = 50.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""
            current_speech_ms = 0.0
            current_speech_rms = 1200.0
            finish_next = False

            def process_chunk(self, frame):
                if self.finish_next:
                    self.finish_next = False
                    self.is_speaking = False
                    self.current_chunk_is_speech = False
                    return SegmentEvent(
                        audio=self.current_audio + frame,
                        duration_ms=self.current_speech_ms + 32,
                    )
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_speech_ms += 32
                self.current_audio += frame
                return None

        async def run():
            clock = FakeClock()
            interruptions = []
            final_calls = []

            async def on_bargein(detected_at):
                interruptions.append(detected_at)

            async def final_stt(audio):
                final_calls.append(audio)
                return "Und morgen?"

            segmenter = SpeechSegmenter()
            wakeword = MagicMock()
            wakeword.check.return_value = False
            session = VoiceStreamSession(
                vad_segmenter=segmenter,
                wakeword_detector=wakeword,
                conversation_active_fn=lambda: True,
                partial_interval_ms=10_000,
                partial_executor_available=lambda: False,
                bargein_active_fn=lambda: True,
                on_bargein=on_bargein,
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

            for _ in range(10):
                await session.handle_chunk(vad_frame(), muted=False)
                clock.advance(0.032)

            assert interruptions == [pytest.approx(50.288)]
            assert segmenter.current_speech_ms == 320
            assert segmenter.current_audio == vad_frame() * 10

            segmenter.finish_next = True
            result = await session.handle_chunk(vad_frame(), muted=False)

            assert result["text"] == "Und morgen?"
            assert final_calls == [segmenter.current_audio + vad_frame()]
            assert len(interruptions) == 1

        monkeypatch.setattr(config, "VOICE_BARGEIN", True, raising=False)
        monkeypatch.setattr(config, "VOICE_BARGEIN_MIN_SPEECH_MS", 300, raising=False)
        monkeypatch.setattr(config, "VOICE_BARGEIN_MIN_RMS", 800, raising=False)
        asyncio.run(run())

    @pytest.mark.parametrize(
        ("enabled", "speech_ms", "rms", "should_interrupt"),
        [(True, 288, 1200, False), (True, 320, 799, False), (False, 320, 1200, False)],
    )
    def test_bargein_rejects_short_quiet_or_disabled_speech(
        self, monkeypatch, enabled, speech_ms, rms, should_interrupt,
    ):
        import config

        class SpeechSegmenter:
            is_speaking = True
            current_chunk_is_speech = True
            current_audio = vad_frame()
            current_speech_rms = rms
            current_speech_ms = speech_ms

            def process_chunk(self, _frame):
                return None

        interruptions = []

        async def on_bargein(detected_at):
            interruptions.append(detected_at)

        monkeypatch.setattr(config, "VOICE_BARGEIN", enabled, raising=False)
        monkeypatch.setattr(config, "VOICE_BARGEIN_MIN_SPEECH_MS", 300, raising=False)
        monkeypatch.setattr(config, "VOICE_BARGEIN_MIN_RMS", 800, raising=False)
        session = VoiceStreamSession(
            vad_segmenter=SpeechSegmenter(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_executor_available=lambda: False,
            bargein_active_fn=lambda: True,
            on_bargein=on_bargein,
            clock=lambda: 7.0,
        )
        asyncio.run(session.handle_chunk(vad_frame(), muted=False))

        assert bool(interruptions) is should_interrupt


    def test_no_result_while_segment_incomplete(self):
        session = make_session(segment_events=[None], wake_hits=[False])
        result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))
        assert result is None

    def test_segment_without_wakeword_or_active_conversation_is_dropped(self):
        segment = SegmentEvent(audio=b"AUDIO", duration_ms=500)
        session = make_session(segment_events=[segment], wake_hits=[False], conversation_active=False)
        result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))
        assert result is None

    def test_segment_with_wakeword_triggers_transcription(self, monkeypatch):
        import core.voice_stream as vs
        segment = SegmentEvent(audio=b"AUDIO", duration_ms=500)
        # WAKEWORD_FRAME_BYTES (2560) spans exactly 2.5 VAD_FRAME_BYTES (1024)
        # frames, so feeding one wake-word frame's worth of bytes completes 2
        # VAD frames along the way; queue enough segment/wake side effects to
        # cover every process_chunk/check call triggered.
        session = make_session(
            segment_events=[None, None, segment],
            wake_hits=[True],
            conversation_active=False,
        )
        monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="hallo mantis"))

        result = asyncio.run(
            session.handle_chunk(wakeword_frame() + vad_frame(), muted=False),
        )

        assert result["text"] == "Mantis, hallo mantis" and result["addressed"] is True
        assert result["wakeword_triggered"] is True
        assert isinstance(result["stt_ms"], float) and result["stt_ms"] >= 0
        assert isinstance(result["turn_started_at"], float)

    def test_wakeword_from_discarded_160ms_segment_cannot_authorize_later_command(
        self, monkeypatch,
    ):
        import config
        import core.voice_stream as vs
        from core.vad import VadSegmenter

        class FakeVAD:
            def speech_probability(self, frame):
                return 1.0 if frame != b"\0" * len(frame) else 0.0

        class OneWakeHit:
            def __init__(self):
                self.calls = 0

            def check(self, _frame):
                self.calls += 1
                return self.calls == 1

        final_calls = []

        async def final_stt(audio):
            final_calls.append(audio)
            return "Mach das Licht an", False

        async def run():
            session = VoiceStreamSession(
                vad_segmenter=VadSegmenter(FakeVAD(), chunk_ms=32),
                wakeword_detector=OneWakeHit(),
                conversation_active_fn=lambda: False,
                partial_executor_available=lambda: False,
            )
            short_speech = b"\xe8\x03" * (160 * 16)
            one_second_silence = b"\0\0" * 16_000
            command_speech = b"\xe8\x03" * (3 * 16_000)

            await session.handle_chunk(short_speech, muted=False)
            for _ in range(120):
                await session.handle_chunk(one_second_silence, muted=False)
            await session.handle_chunk(command_speech, muted=False)
            result = await session.handle_chunk(one_second_silence, muted=False)
            await session.close()
            return result

        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
        monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

        result = asyncio.run(run())

        assert result is None
        assert final_calls == []

    def test_sustained_detector_signal_after_discarded_segment_is_not_a_new_hit(
        self, monkeypatch,
    ):
        import config
        import core.voice_stream as vs
        from core.vad import VadSegmenter

        class FakeVAD:
            def speech_probability(self, frame):
                return 1.0 if frame != b"\0" * len(frame) else 0.0

        class SustainedWake:
            """Detektor bleibt nach einem Treffer True (eine Erkennung, keine neue Flanke)."""

            def __init__(self):
                self.calls = 0

            def check(self, _frame):
                self.calls += 1
                return self.calls <= 200

        final_calls = []

        async def final_stt(audio):
            final_calls.append(audio)
            return "Mach das Licht an", False

        async def run():
            session = VoiceStreamSession(
                vad_segmenter=VadSegmenter(FakeVAD(), chunk_ms=32),
                wakeword_detector=SustainedWake(),
                conversation_active_fn=lambda: False,
                partial_executor_available=lambda: False,
            )
            short_speech = b"\xe8\x03" * (160 * 16)
            silence_40ms = b"\0\0" * 640
            command_speech = b"\xe8\x03" * (3 * 16_000)

            await session.handle_chunk(short_speech, muted=False)
            for _ in range(26):
                await session.handle_chunk(silence_40ms, muted=False)
            await session.handle_chunk(command_speech, muted=False)
            result = None
            for _ in range(30):
                result = result or await session.handle_chunk(silence_40ms, muted=False)
            await session.close()
            return result

        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
        monkeypatch.setattr(config, "VOICE_ADAPTIVE_SILENCE", False, raising=False)
        monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

        result = asyncio.run(run())

        assert result is None or not result.get("wakeword_triggered")

    @pytest.mark.parametrize(
        ("silence_before_speech_ms", "expected_wakeword"),
        [(1000, True), (2000, False)],
        ids=["within-1500ms-preroll", "outside-1500ms-preroll"],
    )
    def test_wakeword_is_bound_to_segment_start_and_configured_preroll(
        self, monkeypatch, silence_before_speech_ms, expected_wakeword,
    ):
        import config
        import core.voice_stream as vs
        from core.vad import VadSegmenter

        class FakeVAD:
            def speech_probability(self, frame):
                return 1.0 if frame != b"\0" * len(frame) else 0.0

        class OneWakeHit:
            def __init__(self):
                self.calls = 0

            def check(self, _frame):
                self.calls += 1
                return self.calls == 1

        async def final_stt(_audio):
            return "Erzähl mir etwas", False

        async def run():
            session = VoiceStreamSession(
                vad_segmenter=VadSegmenter(FakeVAD(), chunk_ms=32),
                wakeword_detector=OneWakeHit(),
                conversation_active_fn=lambda: False,
                partial_executor_available=lambda: False,
            )
            silence = b"\0\0" * 16_000
            for _ in range(silence_before_speech_ms // 1000):
                await session.handle_chunk(silence, muted=False)
            await session.handle_chunk(b"\xe8\x03" * (3 * 16_000), muted=False)
            result = await session.handle_chunk(silence, muted=False)
            await session.close()
            return result

        monkeypatch.setattr(config, "VOICE_WAKE_PREROLL_MS", 1500, raising=False)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
        monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

        result = asyncio.run(run())

        if expected_wakeword:
            assert result is not None
            assert result["wakeword_triggered"] is True
        else:
            assert result is None

    @pytest.mark.parametrize(
        ("wake_after_ms", "speech_frames", "minimum_runs"),
        [
            (400, 375, 2), (800, 375, 2),
            (400, 563, 2), (800, 563, 2),
            (400, 750, 3), (800, 750, 3),
        ],
        ids=[
            "wake-400ms-speech-3s", "wake-800ms-speech-3s",
            "wake-400ms-speech-4_5s", "wake-800ms-speech-4_5s",
            "wake-400ms-speech-6s", "wake-800ms-speech-6s",
        ],
    )
    def test_partial_stt_runs_from_speech_start_with_real_8ms_frames(
        self, monkeypatch, wake_after_ms, speech_frames, minimum_runs,
    ):
        import config
        import core.voice_stream as vs
        from core import stt_runtime
        from core.vad import VadSegmenter

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class FakeVAD:
            def speech_probability(self, frame):
                return 1.0 if frame != b"\0" * len(frame) else 0.0

        class WakeAfterSpeechOffset:
            def __init__(self):
                self.samples_seen = 0
                self.hit = False
                self.speech_start_sample = 384 * 16

            def check(self, frame):
                self.samples_seen += len(frame) // 2
                target = self.speech_start_sample + wake_after_ms * 16
                if not self.hit and self.samples_seen >= target:
                    self.hit = True
                    return True
                return False

        async def run():
            clock = FakeClock()
            partial_audio_seconds = []

            async def partial_stt(audio):
                partial_audio_seconds.append(len(audio) / (2 * 16_000))
                return "Teiltext", False

            segmenter = VadSegmenter(FakeVAD(), chunk_ms=32)
            wakeword = WakeAfterSpeechOffset()
            session = VoiceStreamSession(
                vad_segmenter=segmenter,
                wakeword_detector=wakeword,
                conversation_active_fn=lambda: False,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                partial_interval_ms=700,
                clock=clock,
            )
            raw_frame = lambda speech: (
                b"\xe8\x03" if speech else b"\0\0"
            ) * 128

            async def feed(frames, speech):
                for _ in range(frames):
                    await session.handle_chunk(raw_frame(speech), muted=False)
                    clock.advance(0.008)
                    await asyncio.sleep(0)

            # Twelve 32ms VAD frames are the segmenter's real 384ms preroll.
            await feed(48, False)
            await feed(speech_frames, True)
            # Three trailing 8ms frames may complete the preceding speech VAD
            # frame, so feed 832ms to guarantee 800ms of actual silence frames.
            result = await session.handle_chunk(b"\0\0" * (128 * 104), muted=False)
            await session.close()
            return result, partial_audio_seconds

        monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 900, raising=False)
        monkeypatch.setattr(config, "VOICE_SILENCE_MS", 800, raising=False)
        monkeypatch.setattr(stt_runtime, "_samples", deque([120.0], maxlen=10))
        monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value=("Final", False)))

        result, partial_audio_seconds = asyncio.run(run())

        assert result is not None and result["wakeword_triggered"] is True
        assert result["partial_runs"] >= minimum_runs
        # At an 800ms wakeword delay the first 700ms interval must already have
        # launched from speech onset, before the detector reports the wakeword.
        if wake_after_ms > 700:
            assert partial_audio_seconds[0] < 0.384 + wake_after_ms / 1000

    def test_over_budget_partial_is_retried_after_the_next_interval(self, monkeypatch):
        import config
        import core.voice_stream as vs
        estimates = iter((1000.0, 200.0))

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = True
            current_chunk_is_speech = True
            current_audio = b"\0\0" * 16_000

            def process_chunk(self, _frame):
                return None

        async def run():
            clock = FakeClock()
            partial_calls = []

            async def partial_stt(_audio):
                partial_calls.append(clock.now)
                return "Teiltext"

            session = VoiceStreamSession(
                vad_segmenter=SpeechSegmenter(),
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: False,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                partial_interval_ms=700,
                clock=clock,
            )
            await session.handle_chunk(vad_frame(), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(), muted=False)
            await session.handle_chunk(vad_frame(), muted=False)
            assert partial_calls == []
            assert session._partial_runs == 0
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(), muted=False)
            await asyncio.sleep(0)
            return partial_calls

        monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
        monkeypatch.setattr(
            vs.stt_runtime, "estimate_stt_runtime_ms", lambda _seconds: next(estimates),
        )

        partial_calls = asyncio.run(run())

        assert len(partial_calls) == 1
        assert partial_calls[0] == pytest.approx(1.4)

    def test_segment_during_active_conversation_triggers_even_without_wakeword(self, monkeypatch):
        import core.voice_stream as vs
        segment = SegmentEvent(audio=b"AUDIO", duration_ms=500)
        session = make_session(segment_events=[segment], wake_hits=[False], conversation_active=True)
        monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="und morgen?"))

        result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))

        assert result["text"] == "und morgen?" and result["addressed"] is True
        assert result["wakeword_triggered"] is False
        assert isinstance(result["stt_ms"], float) and result["stt_ms"] >= 0

    def test_small_chunks_are_buffered_until_full_vad_frame(self):
        """Proves the buffering logic: feed several small (8ms-equivalent,
        128-sample/256-byte) chunks that together add up to exactly one VAD
        frame, and confirm process_chunk is invoked exactly once with the
        correctly-sized combined chunk — not once per small input chunk."""
        session = make_session(segment_events=[None], wake_hits=[])

        small_chunk_bytes = 256  # 128 samples * 2 bytes, matches the AudioWorklet's real render-quantum size
        assert VAD_FRAME_BYTES % small_chunk_bytes == 0
        n_chunks = VAD_FRAME_BYTES // small_chunk_bytes

        small_chunks = [bytes([i % 256]) * small_chunk_bytes for i in range(n_chunks)]

        for i, chunk in enumerate(small_chunks[:-1]):
            result = asyncio.run(session.handle_chunk(chunk, muted=False))
            assert result is None
            session._segmenter.process_chunk.assert_not_called()

        # The final small chunk completes exactly one full VAD frame.
        result = asyncio.run(session.handle_chunk(small_chunks[-1], muted=False))
        assert result is None
        session._segmenter.process_chunk.assert_called_once()

        (called_frame,), _ = session._segmenter.process_chunk.call_args
        assert called_frame == b"".join(small_chunks)
        assert len(called_frame) == VAD_FRAME_BYTES

    def test_wakeword_and_vad_buffers_are_independent(self):
        """The wake-word detector requires a larger frame (WAKEWORD_FRAME_BYTES)
        than the VAD segmenter (VAD_FRAME_BYTES). Feeding exactly one VAD frame
        should trigger the VAD segmenter but not (yet) the wake-word detector,
        since its buffer hasn't filled."""
        assert WAKEWORD_FRAME_BYTES > VAD_FRAME_BYTES
        session = make_session(segment_events=[None], wake_hits=[])

        asyncio.run(session.handle_chunk(vad_frame(), muted=False))

        session._segmenter.process_chunk.assert_called_once()
        session._wakeword.check.assert_not_called()

    def test_partial_transcription_is_throttled_and_never_awaited_before_final(self, monkeypatch):
        import core.voice_stream as vs

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""
            finish_next = False

            def process_chunk(self, frame):
                if self.finish_next:
                    self.finish_next = False
                    self.is_speaking = False
                    self.current_chunk_is_speech = False
                    return SegmentEvent(
                        audio=b"FINAL-AUDIO", duration_ms=900,
                        last_speech_sample_index=10_000,
                    )
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            clock = FakeClock()
            partial_started = asyncio.Event()
            release_partial = asyncio.Event()
            partial_calls = []
            final_calls = []

            async def partial_stt(audio):
                partial_calls.append(audio)
                partial_started.set()
                await release_partial.wait()
                return "Mantis, was steht heute an"

            async def final_stt(audio):
                final_calls.append(audio)
                return "Mantis, was steht heute an"

            segmenter = SpeechSegmenter()
            session = VoiceStreamSession(
                vad_segmenter=segmenter,
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=700,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.699)
            await session.handle_chunk(vad_frame(2), muted=False)
            assert partial_calls == []

            clock.advance(0.001)
            await session.handle_chunk(vad_frame(3), muted=False)
            await partial_started.wait()
            assert len(partial_calls) == 1

            clock.advance(0.7)
            await session.handle_chunk(vad_frame(4), muted=False)
            assert len(partial_calls) == 1

            segmenter.finish_next = True
            result = await asyncio.wait_for(
                session.handle_chunk(vad_frame(5), muted=False),
                timeout=0.1,
            )
            assert result["text"] == "Mantis, was steht heute an"
            assert final_calls == [b"FINAL-AUDIO"]
            assert result["partial_runs"] == 1

            release_partial.set()
            await asyncio.sleep(0)
            return partial_calls, result

        partial_calls, _result = asyncio.run(run())
        assert partial_calls == [vad_frame(1) + vad_frame(2) + vad_frame(3)]

    def test_partial_transcription_is_skipped_when_executor_is_busy(self, monkeypatch):
        import core.voice_stream as vs

        class FakeClock:
            now = 10.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                self.current_audio += frame
                self.is_speaking = True
                self.current_chunk_is_speech = True
                return None

        partial_calls = []

        async def partial_stt(audio):
            partial_calls.append(audio)
            return "Teiltext"

        async def final_stt(_audio):
            return "Final"

        clock = FakeClock()
        session = VoiceStreamSession(
            vad_segmenter=SpeechSegmenter(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_interval_ms=1,
            partial_transcriber=partial_stt,
            partial_executor_available=lambda: False,
            clock=clock,
        )
        monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

        async def run():
            await session.handle_chunk(vad_frame(), muted=False)
            clock.advance(0.002)
            return await session.handle_chunk(vad_frame(), muted=False)

        result = asyncio.run(run())

        assert partial_calls == []
        assert result is None

    def test_partial_covering_last_speech_frame_becomes_final_under_shared_stt_lock(
        self, monkeypatch,
    ):
        import core.voice as voice
        import core.voice_stream as vs

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""
            finish_next = False

            def process_chunk(self, frame):
                if self.finish_next:
                    self.finish_next = False
                    self.is_speaking = False
                    self.current_chunk_is_speech = False
                    return SegmentEvent(
                        audio=self.current_audio + frame,
                        duration_ms=900,
                        last_speech_sample_index=len(self.current_audio) // 2,
                    )
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            clock = FakeClock()
            partial_started = asyncio.Event()
            release_partial = asyncio.Event()
            final_calls = []
            monkeypatch.setattr(voice, "_parakeet_unavailable", False)
            monkeypatch.setattr(voice, "_parakeet_model", object())

            async def fake_stt(path):
                if path == "partial":
                    clock.advance(1.4)
                    partial_started.set()
                    await release_partial.wait()
                    return "Mentiones, was steht heute an"
                final_calls.append(path)
                return "Finales Mantis-Transkript"

            async def partial_stt(_audio):
                return await voice.transcribe_audio_partial_with_hint("partial")

            async def final_stt(_audio):
                return await voice.transcribe_audio("final")

            monkeypatch.setattr(voice, "_transcribe_parakeet", fake_stt)
            segmenter = SpeechSegmenter()
            session = VoiceStreamSession(
                vad_segmenter=segmenter,
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=700,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: voice.partial_stt_executor_available(),
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(2), muted=False)
            await partial_started.wait()
            assert voice._whisper_lock.locked()

            segmenter.finish_next = True
            final_task = asyncio.create_task(session.handle_chunk(vad_frame(3), muted=False))
            await asyncio.sleep(0)
            assert voice._whisper_lock.locked()
            release_partial.set()
            result = await final_task

            assert result["text"] == "Mantis, was steht heute an"
            assert result["wakeword_hint"] is True
            assert result["partial_as_final"] is True
            assert result["partial_runs"] == 1
            assert final_calls == []

        asyncio.run(run())

    def test_partial_runtime_estimate_skips_a_run_over_configured_budget(self, monkeypatch):
        import time
        import config
        import core.voice_stream as vs
        from core import voice

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
            monkeypatch.setattr(config, "STT_ENGINE", "parakeet")
            clock = FakeClock()
            partial_calls = []

            class TimedParakeet:
                def transcribe(self, audio_path):
                    partial_calls.append(audio_path)
                    clock.advance(0.8)
                    return MagicMock(text="Teiltext")

            monkeypatch.setattr(voice, "_parakeet_model", TimedParakeet())
            monkeypatch.setattr(voice, "_parakeet_unavailable", False)
            monkeypatch.setattr(
                voice, "_parakeet_last_activity_at", time.monotonic() - 30,
            )
            monkeypatch.setattr(voice, "_whisper_lock", asyncio.Lock())
            monkeypatch.setattr(voice.time, "perf_counter", clock)

            async def final_stt(_audio):
                return "Final"

            session = VoiceStreamSession(
                vad_segmenter=SpeechSegmenter(),
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=700,
                partial_executor_available=lambda: True,
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(2), muted=False)
            assert session._partial_task is not None
            await session._partial_task

            clock.advance(0.7)
            await session.handle_chunk(vad_frame(3), muted=False)
            return partial_calls, session._partial_runs

        partial_calls, partial_runs = asyncio.run(run())

        assert len(partial_calls) == 1
        assert partial_runs == 1

    @pytest.mark.parametrize("outcome", ["success", "none", "error", "cancel"])
    def test_partial_worker_does_not_record_runtime_samples(self, monkeypatch, outcome):
        from core import stt_runtime

        recorded = []
        monkeypatch.setattr(
            stt_runtime, "record_stt_runtime",
            lambda audio_seconds, elapsed_ms: recorded.append((audio_seconds, elapsed_ms)),
        )

        async def partial_stt(_audio):
            if outcome == "error":
                raise RuntimeError("Teil-STT fehlgeschlagen")
            if outcome == "cancel":
                raise asyncio.CancelledError
            if outcome == "success":
                return "Teiltext"
            return None

        session = VoiceStreamSession(
            vad_segmenter=MagicMock(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_transcriber=partial_stt,
        )

        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(session._run_partial(b"\0\0" * 16_000, 0, 16_000))
        elif outcome == "success":
            assert asyncio.run(session._run_partial(b"\0\0" * 16_000, 0, 16_000)) == "Teiltext"
        else:
            assert asyncio.run(session._run_partial(b"\0\0" * 16_000, 0, 16_000)) is None

        assert recorded == []

    def test_initial_runtime_estimate_allows_first_run_within_configured_budget(
        self, monkeypatch,
    ):
        import config
        import core.voice_stream as vs
        from core import stt_runtime

        assert stt_runtime.estimate_stt_runtime_ms(1.25) == 287.5

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b"\0" * 40_000  # 1.25 s vor den zwei Test-Frames

            def process_chunk(self, frame):
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
            clock = FakeClock()
            partial_calls = []

            async def partial_stt(audio):
                partial_calls.append(audio)
                return "Teiltext"

            session = VoiceStreamSession(
                vad_segmenter=SpeechSegmenter(),
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=700,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="Final"))

            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(2), muted=False)
            await asyncio.sleep(0)
            return partial_calls, session._partial_runs

        partial_calls, partial_runs = asyncio.run(run())

        assert len(partial_calls) == 1
        assert partial_runs == 1

    def test_partial_runtime_estimate_is_shared_with_new_sessions(self, monkeypatch):
        import time
        import config
        import core.voice_stream as vs
        from core import stt_runtime, voice

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
            monkeypatch.setattr(config, "STT_ENGINE", "parakeet")
            stt_runtime.record_stt_runtime(
                1.0, stt_runtime.INITIAL_MS_PER_AUDIO_SECOND, replace_samples=True,
            )
            clock = FakeClock()
            partial_calls = []

            class TimedParakeet:
                def transcribe(self, audio_path):
                    partial_calls.append(audio_path)
                    clock.advance(1.6)
                    return MagicMock(text="Teiltext")

            monkeypatch.setattr(voice, "_parakeet_model", TimedParakeet())
            monkeypatch.setattr(voice, "_parakeet_unavailable", False)
            monkeypatch.setattr(
                voice, "_parakeet_last_activity_at", time.monotonic() - 30,
            )
            monkeypatch.setattr(voice, "_whisper_lock", asyncio.Lock())
            monkeypatch.setattr(voice.time, "perf_counter", clock)

            def new_session():
                return VoiceStreamSession(
                    vad_segmenter=SpeechSegmenter(),
                    wakeword_detector=MagicMock(),
                    conversation_active_fn=lambda: True,
                    partial_interval_ms=700,
                    partial_executor_available=lambda: True,
                    clock=clock,
                )

            monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="Final"))
            first_session = new_session()
            await first_session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await first_session.handle_chunk(vad_frame(2), muted=False)
            assert first_session._partial_task is not None
            await first_session._partial_task
            audio_seconds = 2 * len(vad_frame(1)) / (2 * 16_000)
            assert stt_runtime.estimate_stt_runtime_ms(audio_seconds) > 600

            second_session = new_session()
            await second_session.handle_chunk(vad_frame(3), muted=False)
            clock.advance(0.7)
            await second_session.handle_chunk(vad_frame(4), muted=False)

            return partial_calls, first_session._partial_runs, second_session._partial_runs

        partial_calls, first_runs, second_runs = asyncio.run(run())

        assert len(partial_calls) == 1
        assert first_runs == 1
        assert second_runs == 0

    def test_partial_run_counter_changes_only_after_stt_task_is_admitted(self, monkeypatch):
        import pytest
        import core.voice_stream as vs

        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        real_create_task = asyncio.create_task

        def reject_partial_task(coro, *, name=None, context=None):
            if name == "voice-partial-stt":
                coro.close()
                raise RuntimeError("task was not admitted")
            return real_create_task(coro, name=name, context=context)

        monkeypatch.setattr(vs.asyncio, "create_task", reject_partial_task)
        clock = FakeClock()

        async def partial_stt(_audio):
            return "Teiltext"

        session = VoiceStreamSession(
            vad_segmenter=SpeechSegmenter(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_interval_ms=1,
            partial_transcriber=partial_stt,
            partial_executor_available=lambda: True,
            clock=clock,
        )

        async def run():
            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.002)
            with pytest.raises(RuntimeError, match="not admitted"):
                await session.handle_chunk(vad_frame(2), muted=False)

        asyncio.run(run())

        assert session._partial_runs == 0

    def test_partial_transcription_only_runs_on_vad_speech_frames(self):
        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechThenSilence:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                if not self.is_speaking:
                    self.is_speaking = True
                    self.current_chunk_is_speech = True
                    self.current_audio += frame
                else:
                    self.current_chunk_is_speech = False
                return None

        clock = FakeClock()
        partial_calls = []

        async def partial_stt(audio):
            partial_calls.append(audio)
            return "Teiltext"

        session = VoiceStreamSession(
            vad_segmenter=SpeechThenSilence(),
            wakeword_detector=MagicMock(),
            conversation_active_fn=lambda: True,
            partial_interval_ms=700,
            partial_transcriber=partial_stt,
            partial_executor_available=lambda: True,
            clock=clock,
        )

        async def run():
            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(2), muted=False)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert partial_calls == []

    def test_session_close_stops_partial_callbacks_and_cleans_up_task(self):
        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class SpeechSegmenter:
            is_speaking = False
            current_chunk_is_speech = False
            current_audio = b""

            def process_chunk(self, frame):
                self.is_speaking = True
                self.current_chunk_is_speech = True
                self.current_audio += frame
                return None

        async def run():
            clock = FakeClock()
            partial_started = asyncio.Event()
            release_partial = asyncio.Event()
            callbacks = []

            async def partial_stt(_audio):
                partial_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release_partial.wait()
                return "veraltet"

            session = VoiceStreamSession(
                vad_segmenter=SpeechSegmenter(),
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=1,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                on_partial=callbacks.append,
                clock=clock,
            )
            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.002)
            await session.handle_chunk(vad_frame(2), muted=False)
            await partial_started.wait()

            closing = asyncio.create_task(session.close())
            await asyncio.sleep(0)
            assert not closing.done()
            assert callbacks == []
            release_partial.set()
            await closing
            assert session._partial_task is None
            assert callbacks == []
            assert await session.handle_chunk(vad_frame(3), muted=False) is None
            await session.close()

        asyncio.run(run())

    def test_cancelled_native_inference_keeps_shared_lock_until_thread_finishes(
        self, monkeypatch,
    ):
        import threading
        from types import SimpleNamespace

        import pytest
        import config
        import core.voice as voice

        started = threading.Event()
        release = threading.Event()

        class BlockingModel:
            def transcribe(self, _path):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("fake inference timed out")
                return SimpleNamespace(text="Teiltext")

        monkeypatch.setattr(config, "STT_ENGINE", "parakeet")
        monkeypatch.setattr(voice, "_parakeet_unavailable", False)
        monkeypatch.setattr(voice, "_parakeet_model", BlockingModel())

        async def run():
            assert not voice._whisper_lock.locked()
            task = asyncio.create_task(voice.transcribe_audio_partial("fake.wav"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert voice._whisper_lock.locked()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert voice._whisper_lock.locked()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not voice._whisper_lock.locked()
            finally:
                release.set()
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())

    def test_repeated_cancellation_keeps_native_inference_lock_until_thread_finishes(
        self, monkeypatch,
    ):
        import threading
        from types import SimpleNamespace

        import pytest
        import config
        import core.voice as voice

        started = threading.Event()
        release = threading.Event()

        class BlockingModel:
            def transcribe(self, _path):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("fake inference timed out")
                return SimpleNamespace(text="Teiltext")

        monkeypatch.setattr(config, "STT_ENGINE", "parakeet")
        monkeypatch.setattr(voice, "_parakeet_unavailable", False)
        monkeypatch.setattr(voice, "_parakeet_model", BlockingModel())

        async def run():
            task = asyncio.create_task(voice.transcribe_audio_partial("fake.wav"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()

                task.cancel()
                await asyncio.sleep(0)

                assert not task.done()
                assert voice.partial_stt_executor_available() is False
                assert await voice.transcribe_audio_partial("second.wav") is None

                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert voice.partial_stt_executor_available() is True
            finally:
                release.set()
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
