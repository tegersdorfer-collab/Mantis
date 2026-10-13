"""Unit-Tests für core/backends/fallback.py (Failover-Logik, kein LLM nötig)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from core.backends.base import AgentBackend
from core.backends.fallback import FallbackBackend


class FakeBackend(AgentBackend):
    def __init__(self, name="fake", result=("antwort", []), error=None, delay=0.0):
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.calls = 0
        self.warmups = 0

    @property
    def model_name(self):
        return self._name

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result

    async def warmup(self):
        self.warmups += 1


def _call(backend):
    return asyncio.run(backend.call(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        stream_cb=None, temperature=0.7, max_tokens=100,
    ))


class TestHappyPath:
    def test_primary_wins(self):
        primary = FakeBackend("claude", result=("von claude", []))
        fallback = FakeBackend("qwen", result=("von qwen", []))
        fb = FallbackBackend(primary, fallback)
        text, _ = _call(fb)
        assert text == "von claude"
        assert primary.calls == 1
        assert fallback.calls == 0

    def test_model_name_is_primary(self):
        fb = FallbackBackend(FakeBackend("claude"), FakeBackend("qwen"))
        assert fb.model_name == "claude"


class TestFailover:
    def test_primary_error_uses_fallback(self):
        primary = FakeBackend("claude", error=ConnectionError("api down"))
        fallback = FakeBackend("qwen", result=("von qwen", []))
        fb = FallbackBackend(primary, fallback)
        text, _ = _call(fb)
        assert text == "von qwen"
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_primary_timeout_uses_fallback(self):
        primary = FakeBackend("claude", delay=0.2)
        fallback = FakeBackend("qwen", result=("von qwen", []))
        fb = FallbackBackend(primary, fallback, timeout_s=0.05)
        text, _ = _call(fb)
        assert text == "von qwen"

    def test_cooldown_skips_primary(self):
        primary = FakeBackend("claude", error=ConnectionError("api down"))
        fallback = FakeBackend("qwen", result=("von qwen", []))
        fb = FallbackBackend(primary, fallback, cooldown_s=60)
        _call(fb)
        _call(fb)
        # Zweiter Call geht direkt ans Fallback, ohne Primär erneut zu probieren
        assert primary.calls == 1
        assert fallback.calls == 2

    def test_after_cooldown_primary_retried(self):
        primary = FakeBackend("claude", error=ConnectionError("api down"))
        fallback = FakeBackend("qwen", result=("von qwen", []))
        fb = FallbackBackend(primary, fallback, cooldown_s=0.0)
        _call(fb)
        _call(fb)
        # Cooldown 0 → Primär wird jedes Mal wieder probiert
        assert primary.calls == 2

    def test_model_name_during_cooldown(self):
        primary = FakeBackend("claude", error=ConnectionError("api down"))
        fallback = FakeBackend("qwen")
        fb = FallbackBackend(primary, fallback, cooldown_s=60)
        _call(fb)
        assert "Fallback" in fb.model_name
        assert "qwen" in fb.model_name

    def test_both_fail_raises(self):
        primary = FakeBackend("claude", error=ConnectionError("api down"))
        fallback = FakeBackend("qwen", error=RuntimeError("ollama weg"))
        fb = FallbackBackend(primary, fallback)
        try:
            _call(fb)
            assert False, "sollte RuntimeError werfen"
        except RuntimeError:
            pass

    def test_empty_timeout_message_is_recorded_with_exception_type(self, monkeypatch, caplog):
        import logging

        events = []
        monkeypatch.setattr(
            "core.db.log_error",
            lambda source, exc: events.append((source, str(exc))),
        )
        fb = FallbackBackend(
            FakeBackend("claude", error=asyncio.TimeoutError()),
            FakeBackend("qwen"),
        )

        with caplog.at_level(logging.WARNING, logger="core.backends.fallback"):
            _call(fb)

        assert events == [("agent_fallback", "TimeoutError")]
        assert "TimeoutError" in caplog.text


class TestWarmup:
    def test_warms_both(self):
        primary = FakeBackend("claude")
        fallback = FakeBackend("qwen")
        fb = FallbackBackend(primary, fallback)
        asyncio.run(fb.warmup())
        assert primary.warmups == 1
        assert fallback.warmups == 1


def test_three_stage_chain_falls_through_timeout_and_http_error_then_cools_down(caplog):
    import httpx
    import logging

    primary = FakeBackend("openrouter-primary", delay=0.05)
    secondary = FakeBackend(
        "openrouter-fallback",
        error=httpx.HTTPStatusError(
            "bad request",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
            response=httpx.Response(400),
        ),
    )
    local = FakeBackend("ollama", result=("lokal", []))
    backend = FallbackBackend(
        backends=[primary, secondary, local],
        stage_timeouts=[0.005, 0.005, None],
        cooldown_s=60,
    )

    with caplog.at_level(logging.WARNING, logger="core.backends.fallback"):
        assert _call(backend) == ("lokal", [])
        assert _call(backend) == ("lokal", [])

    assert primary.calls == 1
    assert secondary.calls == 1
    assert local.calls == 2
    assert "TimeoutError" in caplog.text
    assert "HTTPStatusError" in caplog.text


def test_chain_uses_next_available_cloud_stage_during_primary_cooldown():
    primary = FakeBackend("openrouter-primary", error=ConnectionError("offline"))
    secondary = FakeBackend("openrouter-fallback", result=("cloud fallback", []))
    local = FakeBackend("ollama")
    backend = FallbackBackend(
        backends=[primary, secondary, local],
        stage_timeouts=[0.1, 0.1, None],
        cooldown_s=60,
    )

    assert _call(backend) == ("cloud fallback", [])
    assert _call(backend) == ("cloud fallback", [])
    assert primary.calls == 1
    assert secondary.calls == 2
    assert local.calls == 0
