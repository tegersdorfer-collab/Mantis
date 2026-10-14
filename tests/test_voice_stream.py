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
