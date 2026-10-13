import asyncio
from pathlib import Path
import secrets
from types import SimpleNamespace

from core.backends.base import AgentBackend
from core.backends.fallback import FallbackBackend
from core.backends.factory import (
    build_chat_backend,
    build_chat_task_provider,
    describe_backend_chain,
)
from settings import MantisSettings


class FakeBackend(AgentBackend):
    def __init__(self, provider, model):
        self.provider_name = provider
        self._model = model
        self.kwargs = None
        self.calls = []

    @property
    def model_name(self):
        return self._model

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return "ok", []

    async def warmup(self):
        return None


def _cfg(**overrides):
    values = {
        "LLM_LOCAL_ONLY": False,
        "OPENROUTER_API_KEY": "openrouter-test-key",
        "CHAT_CLOUD_MODEL": "openai/gpt-6-luna",
        "CHAT_CLOUD_FALLBACK_MODEL": "google/gemini-3.1-flash-lite",
        "CHAT_CLOUD_REASONING_EFFORT": "low",
        "CHAT_CLOUD_TIMEOUT_S": 30,
        "CHAT_ANTHROPIC_MODEL": "claude-haiku-test",
        "CHAT_CHATGPT_PLAN_ENABLED": True,
        "CHAT_CHATGPT_PLAN_MODEL": "gpt-6-luna",
        "CHATGPT_AUTH_FILE": "/tmp/mantis-chatgpt-auth-test.json",
        "CHATGPT_AUTH_CALLBACK_PORT": 1456,
        "BG_CHAT_TASKS_USE_CLOUD": True,
        "ANTHROPIC_API_KEY": "anthropic-test-key",
        "CLAUDE_CHAT_MODEL": "claude-haiku-test",
        "AGENT_MODEL_STRONG": "local-agent",
        "OLLAMA_KEEP_ALIVE": "-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_fake_backends(monkeypatch):
    import core.backends.factory as factory

    created = []

    def openrouter(model, **kwargs):
        backend = FakeBackend("OpenRouter", model)
        backend.kwargs = kwargs
        backend.purpose = kwargs.get("purpose", "chat-agent")
        created.append(("openrouter", backend))
        return backend

    def ollama(model=None, keep_alive=None):
        backend = FakeBackend("Ollama", model or "local-default")
        backend.kwargs = {"keep_alive": keep_alive}
        created.append(("ollama", backend))
        return backend

    def claude(model=None, purpose="chat-agent"):
        backend = FakeBackend("Claude", model or "claude-default")
        backend.kwargs = {"purpose": purpose}
        backend.purpose = purpose
        created.append(("claude", backend))
        return backend

    def chatgpt(model=None, auth_file=None, purpose="chat-agent"):
        backend = FakeBackend("ChatGPT-Abo", model or "gpt-6-luna")
        backend.kwargs = {"auth_file": auth_file, "purpose": purpose}
        created.append(("chatgpt", backend))
        return backend

    monkeypatch.setattr(factory, "OpenRouterBackend", openrouter)
    monkeypatch.setattr(factory, "OllamaBackend", ollama)
    monkeypatch.setattr(factory, "ClaudeBackend", claude)
    monkeypatch.setattr(factory, "ChatGPTPlanBackend", chatgpt)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: False)
    return created


def test_chat_cloud_settings_have_benchmarked_defaults(monkeypatch):
    for name in (
        "CHAT_CLOUD_MODEL",
        "CHAT_CLOUD_FALLBACK_MODEL",
        "CHAT_CLOUD_REASONING_EFFORT",
        "CHAT_CLOUD_TIMEOUT_S",
        "CHAT_ANTHROPIC_MODEL",
        "CHAT_CHATGPT_PLAN_ENABLED",
        "CHAT_CHATGPT_PLAN_MODEL",
        "CHATGPT_AUTH_FILE",
        "CHATGPT_AUTH_CALLBACK_PORT",
        "BG_CHAT_TASKS_USE_CLOUD",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = MantisSettings(_env_file=None)

    assert settings.CHAT_CLOUD_MODEL == "openai/gpt-6-luna"
    assert settings.CHAT_CLOUD_FALLBACK_MODEL == ""
    assert settings.CHAT_CLOUD_REASONING_EFFORT == "low"
    assert settings.CHAT_CLOUD_TIMEOUT_S == 30
    assert settings.CHAT_ANTHROPIC_MODEL == "claude-haiku-5-5"
    assert settings.CHAT_CHATGPT_PLAN_ENABLED is True
    assert settings.CHAT_CHATGPT_PLAN_MODEL == "gpt-6-luna"
    assert settings.CHATGPT_AUTH_FILE == "~/.config/mantis/chatgpt_auth.json"
    assert settings.CHATGPT_AUTH_CALLBACK_PORT == 1456
    assert settings.BG_CHAT_TASKS_USE_CLOUD is True


def test_chat_cloud_settings_can_be_overridden_from_environment(monkeypatch):
    monkeypatch.setenv("CHAT_CLOUD_MODEL", "vendor/model-test")
    monkeypatch.setenv("CHAT_CLOUD_FALLBACK_MODEL", "")
    monkeypatch.setenv("CHAT_CLOUD_TIMEOUT_S", "17")
    monkeypatch.setenv("BG_CHAT_TASKS_USE_CLOUD", "false")

    settings = MantisSettings(_env_file=None)

    assert settings.CHAT_CLOUD_MODEL == "vendor/model-test"
    assert settings.CHAT_CLOUD_FALLBACK_MODEL == ""
    assert settings.CHAT_CLOUD_TIMEOUT_S == 17
    assert settings.BG_CHAT_TASKS_USE_CLOUD is False


def test_chat_chain_settings_can_be_overridden_from_environment(monkeypatch):
    monkeypatch.setenv("CHAT_ANTHROPIC_MODEL", "claude-haiku-custom")
    monkeypatch.setenv("CHAT_CHATGPT_PLAN_ENABLED", "false")
    monkeypatch.setenv("CHAT_CHATGPT_PLAN_MODEL", "gpt-custom")
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "/tmp/chatgpt-auth.json")
    monkeypatch.setenv("CHATGPT_AUTH_CALLBACK_PORT", "1457")

    settings = MantisSettings(_env_file=None)

    assert settings.CHAT_ANTHROPIC_MODEL == "claude-haiku-custom"
    assert settings.CHAT_CHATGPT_PLAN_ENABLED is False
    assert settings.CHAT_CHATGPT_PLAN_MODEL == "gpt-custom"
    assert settings.CHATGPT_AUTH_FILE == "/tmp/chatgpt-auth.json"
    assert settings.CHATGPT_AUTH_CALLBACK_PORT == 1457


def test_cloud_chain_uses_primary_fallback_then_local(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg())

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
        "local-default",
    )
    assert backend.stage_timeouts == (30, 30, 30, None)
    assert describe_backend_chain(backend) == (
        "OpenRouter openai/gpt-6-luna → OpenRouter google/gemini-3.1-flash-lite "
        "→ Claude claude-haiku-test → Ollama local-default"
    )


def test_full_chain_orders_openrouter_anthropic_chatgpt_then_local(monkeypatch):
    import core.backends.factory as factory

    created = _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: True)

    backend = build_chat_backend(_cfg())

    assert [kind for kind, _ in created] == ["openrouter", "openrouter", "claude", "chatgpt", "ollama"]
    assert backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
        "gpt-6-luna",
        "local-default",
    )
    assert backend.stage_timeouts == (30, 30, 30, 30, None)
    assert describe_backend_chain(backend) == (
        "OpenRouter openai/gpt-6-luna → OpenRouter google/gemini-3.1-flash-lite "
        "→ Claude claude-haiku-test → ChatGPT-Abo gpt-6-luna → Ollama local-default"
    )
    assert created[3][1].kwargs == {
        "auth_file": Path("/tmp/mantis-chatgpt-auth-test.json"),
        "purpose": "chat-agent",
    }


def test_local_only_ignores_openrouter_and_claude_keys(monkeypatch):
    created = _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(LLM_LOCAL_ONLY=True))

    assert not isinstance(backend, FallbackBackend)
    assert backend.model_name == "local-agent"
    assert [kind for kind, _ in created] == ["ollama"]
    assert describe_backend_chain(backend) == "Ollama local-agent"


def test_local_only_also_disables_background_cloud_when_flag_is_enabled(monkeypatch):
    created = _install_fake_backends(monkeypatch)

    provider = build_chat_task_provider(_cfg(
        LLM_LOCAL_ONLY=True,
        BG_CHAT_TASKS_USE_CLOUD=True,
    ))

    assert provider is None
    assert created == []


def test_missing_openrouter_key_preserves_claude_fallback(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(OPENROUTER_API_KEY=""))

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == ("claude-haiku-test", "local-default")
    assert describe_backend_chain(backend) == "Claude claude-haiku-test → Ollama local-default"


def test_empty_primary_model_disables_openrouter_and_preserves_claude(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(CHAT_CLOUD_MODEL=""))

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == ("claude-haiku-test", "local-default")


def test_blank_openrouter_key_is_treated_as_missing(monkeypatch):
    created = _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(OPENROUTER_API_KEY="  ", ANTHROPIC_API_KEY=""))

    assert backend.model_name == "local-agent"
    assert [kind for kind, _ in created] == ["ollama"]


def test_chat_task_provider_respects_cloud_flag(monkeypatch):
    _install_fake_backends(monkeypatch)

    provider = build_chat_task_provider(_cfg(BG_CHAT_TASKS_USE_CLOUD=True))
    disabled = build_chat_task_provider(_cfg(BG_CHAT_TASKS_USE_CLOUD=False))

    assert provider is not None
    assert provider.backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
        "local-default",
    )
    assert [stage.purpose for stage in provider.backend.backends[:-1]] == [
        "background",
        "background",
        "background",
    ]
    assert disabled is None


def test_background_chat_chain_includes_anthropic_and_chatgpt_in_order(monkeypatch):
    import core.backends.factory as factory

    _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: True)

    provider = build_chat_task_provider(_cfg())

    assert provider.backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
        "gpt-6-luna",
        "local-default",
    )
    assert [stage.kwargs.get("purpose", "chat-agent") for stage in provider.backend.backends[:-1]] == [
        "background", "background", "background", "background",
    ]


def test_missing_chatgpt_login_is_reported_and_stage_is_skipped(monkeypatch, caplog):
    _install_fake_backends(monkeypatch)
    caplog.set_level("INFO")

    backend = build_chat_backend(_cfg())

    assert backend.backend_chain == (
        "openai/gpt-6-luna", "google/gemini-3.1-flash-lite", "claude-haiku-test", "local-default",
    )
    assert "ChatGPT-Abo übersprungen" in caplog.text


def test_chatgpt_stage_is_skipped_when_auth_scope_lacks_direct_usage(tmp_path, monkeypatch):
    from core.auth.chatgpt import save_auth

    path = tmp_path / "chatgpt_auth.json"
    save_auth(path, {
        "client_id": "oaiapp_test", "ext_agent_host_id": "host-test",
        "access_token": secrets.token_urlsafe(32), "refresh_token": secrets.token_urlsafe(32),
        "expires_at": 9999999999, "scope": "openid profile offline_access",
    })
    backend = build_chat_backend(_cfg(
        OPENROUTER_API_KEY="", ANTHROPIC_API_KEY="", CHATGPT_AUTH_FILE=str(path),
    ))

    assert not isinstance(backend, FallbackBackend)
    assert backend.model_name == "local-agent"


def test_chat_task_provider_adapter_uses_background_chain_for_complete(monkeypatch):
    _install_fake_backends(monkeypatch)
    provider = build_chat_task_provider(_cfg())

    result = asyncio.run(provider.complete("Fasse den Turn zusammen", max_tokens=300))

    assert result == "ok"
    first_stage = provider.backend.backends[0]
    assert first_stage.kwargs["purpose"] == "background"
    assert first_stage.calls[0]["messages"] == [
        {"role": "user", "content": "Fasse den Turn zusammen"},
    ]
    assert first_stage.calls[0]["tools"] is None
    assert first_stage.calls[0]["max_tokens"] == 300


def test_empty_cloud_fallback_uses_primary_then_local(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(CHAT_CLOUD_FALLBACK_MODEL=""))

    assert backend.backend_chain == ("openai/gpt-6-luna", "claude-haiku-test", "local-default")
    assert backend.stage_timeouts == (30, 30, None)
