import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, MagicMock

from core.voice_stream import VoiceStreamSession, VAD_FRAME_BYTES, WAKEWORD_FRAME_BYTES
from core.vad import SegmentEvent


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




class TestVoiceStreamSession:
    def test_ignores_muted_chunks(self):
        session = make_session(segment_events=[], wake_hits=[])
        result = asyncio.run(session.handle_chunk(b"\x00\x00", muted=True))
        assert result is None

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
            segment_events=[None, segment],
            wake_hits=[True],
            conversation_active=False,
        )
        monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="hallo mantis"))

        result = asyncio.run(session.handle_chunk(wakeword_frame(), muted=False))

        assert result["text"] == "hallo mantis" and result["addressed"] is True
        assert isinstance(result["stt_ms"], float) and result["stt_ms"] >= 0
        assert isinstance(result["turn_started_at"], float)

    def test_segment_during_active_conversation_triggers_even_without_wakeword(self, monkeypatch):
        import core.voice_stream as vs
        segment = SegmentEvent(audio=b"AUDIO", duration_ms=500)
        session = make_session(segment_events=[segment], wake_hits=[False], conversation_active=True)
        monkeypatch.setattr(vs, "transcribe_pcm", AsyncMock(return_value="und morgen?"))

        result = asyncio.run(session.handle_chunk(vad_frame(), muted=False))

        assert result["text"] == "und morgen?" and result["addressed"] is True
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
                    return "Mantis, was steht heute an"
                final_calls.append(path)
                return "Finales Mantis-Transkript"

            async def partial_stt(_audio):
                return await voice.transcribe_audio_partial("partial")

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
            assert result["partial_as_final"] is True
            assert result["partial_runs"] == 1
            assert final_calls == []

        asyncio.run(run())

    def test_partial_runtime_estimate_skips_a_run_over_configured_budget(self, monkeypatch):
        import config
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

        async def run():
            monkeypatch.setattr(config, "VOICE_PARTIAL_MAX_MS", 600, raising=False)
            clock = FakeClock()
            partial_calls = []

            async def partial_stt(audio):
                partial_calls.append(audio)
                clock.advance(0.8)
                return "Teiltext"

            async def final_stt(_audio):
                return "Final"

            session = VoiceStreamSession(
                vad_segmenter=SpeechSegmenter(),
                wakeword_detector=MagicMock(),
                conversation_active_fn=lambda: True,
                partial_interval_ms=700,
                partial_transcriber=partial_stt,
                partial_executor_available=lambda: True,
                clock=clock,
            )
            monkeypatch.setattr(vs, "transcribe_pcm", final_stt)

            await session.handle_chunk(vad_frame(1), muted=False)
            clock.advance(0.7)
            await session.handle_chunk(vad_frame(2), muted=False)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            clock.advance(0.7)
            await session.handle_chunk(vad_frame(3), muted=False)
            return partial_calls, session._partial_runs

        partial_calls, partial_runs = asyncio.run(run())

        assert len(partial_calls) == 1
        assert partial_runs == 1

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
