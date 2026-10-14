"""Unit-Tests für core/backends/fallback.py (Failover-Logik, kein LLM nötig)."""
import sys, os
import json
import threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest

from core.backends.base import AgentBackend
from core.backends.fallback import AllBackendsFailedError, FallbackBackend
from core.agent import Agent


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


@pytest.fixture(autouse=True)
def _avoid_real_database_logging(monkeypatch):
    monkeypatch.setattr("core.db.log_error", lambda *args, **kwargs: None)


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

    def test_stream_failover_aborts_old_attempt_and_starts_a_new_one(self):
        class PartialFailure(FakeBackend):
            async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                           think=False, force_tool_call=False):
                self.calls += 1
                await stream_cb("Unfertiger Entwurf")
                raise ConnectionError("provider abgebrochen")

        class StreamSuccess(FakeBackend):
            async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                           think=False, force_tool_call=False):
                self.calls += 1
                await stream_cb("Fertige Antwort.")
                return "Fertige Antwort.", []

        fb = FallbackBackend(
            PartialFailure("openrouter"), StreamSuccess("anthropic"), cooldown_s=0,
        )
        events = []

        async def collect(event):
            events.append(event)

        answer, _ = asyncio.run(Agent(fb).run(
            [{"role": "user", "content": "sag etwas"}], system="s",
            use_tools=False, stream_cb=collect,
        ))

        assert answer == "Fertige Antwort."
        assert [getattr(event, "kind", None) for event in events] == [
            "attempt_start", "delta", "attempt_abort",
            "attempt_start", "delta", "attempt_end",
        ]
        assert events[1].text_delta == "Unfertiger Entwurf"
        assert events[4].text_delta == "Fertige Antwort."
        assert events[0].attempt_id != events[3].attempt_id
        assert events[-1].final is True

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
            response=httpx.Response(503),
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


def test_single_configured_cloud_stage_can_run_without_a_fake_fallback():
    only_provider = FakeBackend("claude", result=("cloud answer", []))
    backend = FallbackBackend(backends=[only_provider], stage_timeouts=[0.5])

    assert _call(backend) == ("cloud answer", [])
    assert only_provider.calls == 1


def test_all_backend_failures_raise_a_clear_exhaustion_error():
    backend = FallbackBackend(
        backends=[
            FakeBackend("openrouter", error=ConnectionError("offline")),
            FakeBackend("anthropic", error=RuntimeError("unavailable")),
        ],
        stage_timeouts=[0.5, 0.5],
        cooldown_s=0,
    )

    with pytest.raises(AllBackendsFailedError, match="Alle Backend-Stufen sind fehlgeschlagen"):
        _call(backend)


def test_empty_visible_answer_uses_next_provider():
    empty = FakeBackend("openrouter", result=("", []))
    fallback = FakeBackend("anthropic", result=("sichtbare Antwort", []))
    backend = FallbackBackend(
        backends=[empty, fallback], stage_timeouts=[0.5, 0.5], cooldown_s=60,
    )

    assert _call(backend) == ("sichtbare Antwort", [])
    assert empty.calls == 1
    assert fallback.calls == 1


def test_truncated_voice_answer_without_sentence_punctuation_keeps_primary():
    from core.voice_text import last_complete_sentence

    class TruncatedVoiceBackend(FakeBackend):
        async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                       think=False, force_tool_call=False):
            self.calls += 1
            return last_complete_sentence("am 12. Oktober"), []

    primary = TruncatedVoiceBackend("openrouter")
    fallback = FakeBackend("anthropic", result=("Fallback-Antwort", []))
    backend = FallbackBackend(primary, fallback, cooldown_s=60)

    assert _call(backend) == ("am 12. Oktober", [])
    assert primary.calls == 1
    assert fallback.calls == 0


def test_anthropic_unsupported_parameter_400_falls_through_without_permanent_disable():
    import httpx

    error = httpx.HTTPStatusError(
        "temperature is deprecated for this model",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        response=httpx.Response(400),
    )
    anthropic = FakeBackend("claude-haiku-5-5", error=error)
    fallback = FakeBackend("chatgpt-plan", result=("fallback", []))
    backend = FallbackBackend(
        backends=[anthropic, fallback], stage_timeouts=[0.5, 0.5], cooldown_s=180,
    )

    assert _call(backend) == ("fallback", [])
    assert anthropic.calls == 1
    assert fallback.calls == 1
    assert backend._down_until[0] != float("inf")


def test_request_validation_error_does_not_cool_down_provider_for_following_calls():
    import httpx

    error = httpx.HTTPStatusError(
        "invalid request structure",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        response=httpx.Response(400),
    )
    primary = FakeBackend("claude-haiku-5-5", error=error)
    fallback = FakeBackend("chatgpt-plan", result=("fallback", []))
    backend = FallbackBackend(
        backends=[primary, fallback], stage_timeouts=[0.5, 0.5], cooldown_s=180,
    )

    assert _call(backend) == ("fallback", [])
    assert _call(backend) == ("fallback", [])

    assert primary.calls == 2
    assert fallback.calls == 2


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


@pytest.mark.parametrize("cloud_stage", [0, 1], ids=["primary", "secondary"])
@pytest.mark.parametrize(
    "failure",
    ["timeout", "4xx", "5xx", "network", "stream-abort", "broken-tool-arguments"],
)
def test_each_openrouter_stage_falls_back_on_backend_errors(cloud_stage, failure):
    import httpx
    from core.backends.openrouter import OpenRouterBackend

    class AbortedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"choices": [{"delta": {"content": "partial"}}]}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            raise httpx.ReadError("stream ended early")

        async def aclose(self):
            pass

    def backend(stage):
        def handler(request):
            request_model = json.loads(request.content)["model"]
            assert request_model == f"cloud-{stage}"
            if stage < cloud_stage:
                raise httpx.ConnectError("advance to secondary", request=request)
            if stage > cloud_stage:
                done = {"choices": [{"delta": {"content": "cloud fallback"}, "finish_reason": "stop"}]}
                body = f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n"
                return httpx.Response(200, content=body.encode())
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure == "4xx":
                error_body = 'data: {"choices":[{"delta":{"content":"bad status accepted"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(429, content=error_body.encode())
            if failure == "5xx":
                error_body = 'data: {"choices":[{"delta":{"content":"bad status accepted"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(500, content=error_body.encode())
            if failure == "network":
                raise httpx.ConnectError("network unavailable", request=request)
            if failure == "stream-abort":
                return httpx.Response(200, stream=AbortedStream())
            chunk = {
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call-invalid",
                        "function": {"name": "noop", "arguments": "[]"},
                    }]},
                    "finish_reason": "tool_calls",
                }],
            }
            return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

        return OpenRouterBackend(
            model=f"cloud-{stage}", api_key="synthetic-key",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://openrouter.ai/api/v1",
            ),
        )

    primary = backend(0)
    secondary = backend(1)
    local = FakeBackend("ollama", result=("lokal", []))
    chain = FallbackBackend(
        backends=[primary, secondary, local],
        stage_timeouts=[0.5, 0.5, None],
        cooldown_s=0,
    )

    result = asyncio.run(chain.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        stream_cb=lambda _: asyncio.sleep(0), temperature=0.5, max_tokens=50,
    ))

    if cloud_stage == 0:
        assert result == ("cloud fallback", [])
        assert local.calls == 0
    else:
        assert result == ("lokal", [])
        assert local.calls == 1


def test_malformed_tool_arguments_never_reach_tool_execution(monkeypatch):
    import httpx
    from core.agent import Agent
    from core.backends.openrouter import OpenRouterBackend
    from core import tools as toolreg

    def bad_backend(model):
        def handler(request):
            message = {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-invalid",
                    "type": "function",
                    "function": {"name": "dangerous", "arguments": "[]"},
                }],
            }
            return httpx.Response(200, json={
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            })

        return OpenRouterBackend(
            model=model, api_key="synthetic-key",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://openrouter.ai/api/v1",
            ),
        )

    executed = []
    async def execute(name, args):
        executed.append((name, args))
        return "ran"

    monkeypatch.setattr(toolreg, "execute", execute)
    chain = FallbackBackend(
        backends=[bad_backend("cloud-1"), bad_backend("cloud-2"), FakeBackend("ollama", result=("local", []))],
        stage_timeouts=[0.5, 0.5, None],
        cooldown_s=0,
    )

    text, trace = asyncio.run(Agent(chain).run(
        messages=[{"role": "user", "content": "run it"}],
        system="test",
        allowed_tools=["dangerous"],
    ))

    assert text == "local"
    assert trace == []
    assert executed == []


def test_failover_redacts_configured_keys_and_bearer_tokens(monkeypatch, caplog):
    import config

    openrouter_key = "synthetic-openrouter-secret"
    anthropic_key = "synthetic-anthropic-secret"
    bearer = "synthetic-bearer-secret"
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", f" {openrouter_key}\n")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", anthropic_key)
    events = []
    monkeypatch.setattr("core.db.log_error", lambda source, exc: events.append(str(exc)))
    raw = f"key={openrouter_key}\nkey={anthropic_key}\nAuthorization: Bearer {bearer}"
    chain = FallbackBackend(
        FakeBackend("cloud", error=RuntimeError(raw)),
        FakeBackend("local"),
        cooldown_s=0,
    )

    with caplog.at_level("WARNING", logger="core.backends.fallback"):
        _call(chain)

    all_error_text = caplog.text + " ".join(events)
    assert openrouter_key not in all_error_text
    assert anthropic_key not in all_error_text
    assert bearer not in all_error_text
    assert "[REDACTED]" in all_error_text


def test_failover_database_logging_does_not_block_fallback(monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def slow_log_error(source, exc):
        started.set()
        release.wait(timeout=1)
        finished.set()

    monkeypatch.setattr("core.db.log_error", slow_log_error)
    chain = FallbackBackend(
        FakeBackend("cloud", error=ConnectionError("offline")),
        FakeBackend("local", result=("survived", [])),
        cooldown_s=0,
    )

    async def exercise():
        result = await chain.call(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            stream_cb=None, temperature=0.7, max_tokens=100,
        )
        assert result == ("survived", [])
        assert await asyncio.to_thread(started.wait, 0.5)
        assert not finished.is_set()
        release.set()

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_stream_failover_replaces_partial_message_instead_of_appending():
    from core.message_handler import MessageHandler

    class StreamingChannel:
        supports_streaming = True

        async def start_message(self, text):
            return 77

        async def edit_message(self, message_id, text, **kwargs):
            edits.append((message_id, text))

    edits = []
    handler = object.__new__(MessageHandler)
    handler.channel = StreamingChannel()
    stream_cb, finalize = asyncio.run(handler._make_stream())

    class PartialThenFailure(FakeBackend):
        async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                       think=False, force_tool_call=False):
            self.calls += 1
            await stream_cb("partial from cloud")
            raise ConnectionError("stream failed")

    class Replacement(FakeBackend):
        async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                       think=False, force_tool_call=False):
            self.calls += 1
            await stream_cb("complete local answer")
            return "complete local answer", []

    chain = FallbackBackend(PartialThenFailure("cloud"), Replacement("local"), cooldown_s=0)
    result = asyncio.run(chain.call(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        stream_cb=stream_cb, temperature=0.5, max_tokens=50,
    ))
    asyncio.run(finalize(result[0]))

    assert result == ("complete local answer", [])
    assert edits[-1] == (77, "complete local answer")
    assert all(message_id == 77 for message_id, _ in edits)


def test_backend_error_can_set_a_longer_fallback_pause():
    class PausedError(RuntimeError):
        fallback_cooldown_s = 1800
        disable_backend = False

    primary = FakeBackend("chatgpt-plan", error=PausedError("limit"))
    chain = FallbackBackend(primary, FakeBackend("local"), cooldown_s=1)

    assert _call(chain) == ("antwort", [])
    assert chain._down_until[0] > __import__("time").time() + 1700


def test_backend_error_can_disable_a_stage_until_chain_is_rebuilt():
    class PermanentError(RuntimeError):
        fallback_cooldown_s = None
        disable_backend = True

    primary = FakeBackend("chatgpt-plan", error=PermanentError("re-login"))
    local = FakeBackend("local")
    chain = FallbackBackend(primary, local, cooldown_s=1)

    assert _call(chain) == ("antwort", [])
    assert chain._down_until[0] == float("inf")
    assert _call(chain) == ("antwort", [])
    assert primary.calls == 1
    assert local.calls == 2
