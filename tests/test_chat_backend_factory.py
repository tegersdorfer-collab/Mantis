import asyncio
from pathlib import Path
import secrets
from types import SimpleNamespace
import logging

import pytest

from core.backends.base import AgentBackend
from core.backends.fallback import FallbackBackend
from core.backends.llm_adapter import BackendLLMAdapter
from core.backends.factory import (
    CloudChainUnavailableError,
    build_chat_backend,
    build_chat_task_provider,
    cloud_chain,
    describe_backend_chain,
    warn_if_local_only,
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

    @property
    def supports_vision(self):
        return self.provider_name != "OpenRouter" or "gpt-6-luna" in self._model

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
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "gemini-3-flash",
        "GEMINI_USD_PER_MTOK_IN": 0.30,
        "GEMINI_USD_PER_MTOK_OUT": 2.50,
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

    def gemini(model=None, api_key=None, purpose="chat-agent"):
        backend = FakeBackend("Gemini", model or "gemini-3-flash")
        backend.kwargs = {"api_key": api_key, "purpose": purpose}
        created.append(("gemini", backend))
        return backend

    monkeypatch.setattr(factory, "OpenRouterBackend", openrouter)
    monkeypatch.setattr(factory, "ClaudeBackend", claude)
    monkeypatch.setattr(factory, "ChatGPTPlanBackend", chatgpt)
    monkeypatch.setattr(factory, "GeminiBackend", gemini)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: False)
    return created


def test_chat_cloud_settings_have_benchmarked_defaults(monkeypatch):
    for name in (
        "CHAT_CLOUD_MODEL",
        "CHAT_CLOUD_FALLBACK_MODEL",
        "CHAT_CLOUD_REASONING_EFFORT",
        "CHAT_CLOUD_TIMEOUT_S",
        "FAST_CHAIN_ORDER",
        "VOICE_CHAIN_ORDER",
        "CHAT_AGENT_CHAIN_ORDER",
        "BACKGROUND_CHAIN_ORDER",
        "VISION_CHAIN_ORDER",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_USD_PER_MTOK_IN",
        "GEMINI_USD_PER_MTOK_OUT",
        "CHAT_ANTHROPIC_MODEL",
        "CHAT_CHATGPT_PLAN_ENABLED",
        "CHAT_CHATGPT_PLAN_MODEL",
        "CHATGPT_AUTH_FILE",
        "CHATGPT_AUTH_CALLBACK_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = MantisSettings(_env_file=None)

    assert settings.CHAT_CLOUD_MODEL == "openai/gpt-6-luna"
    assert settings.CHAT_CLOUD_FALLBACK_MODEL == ""
    assert settings.CHAT_CLOUD_REASONING_EFFORT == "low"
    assert settings.CHAT_CLOUD_TIMEOUT_S == 30
    assert settings.FAST_CHAIN_ORDER == ["openrouter", "anthropic", "chatgpt-plan"]
    assert settings.VOICE_CHAIN_ORDER == ["openrouter", "anthropic", "chatgpt-plan"]
    assert settings.CHAT_AGENT_CHAIN_ORDER == ["openrouter", "anthropic", "chatgpt-plan"]
    assert settings.BACKGROUND_CHAIN_ORDER == ["openrouter", "anthropic", "chatgpt-plan"]
    assert settings.VISION_CHAIN_ORDER == ["openrouter", "anthropic", "chatgpt-plan"]
    assert settings.GEMINI_API_KEY == ""
    assert settings.GEMINI_MODEL == "gemini-3-flash"
    assert settings.GEMINI_USD_PER_MTOK_IN == 0.30
    assert settings.GEMINI_USD_PER_MTOK_OUT == 2.50
    assert settings.CHAT_ANTHROPIC_MODEL == "claude-haiku-5-5"
    assert settings.CHAT_CHATGPT_PLAN_ENABLED is True
    assert settings.CHAT_CHATGPT_PLAN_MODEL == "gpt-6-luna"
    assert settings.CHATGPT_AUTH_FILE == "~/.config/mantis/chatgpt_auth.json"
    assert settings.CHATGPT_AUTH_CALLBACK_PORT == 1456


def test_chat_cloud_settings_can_be_overridden_from_environment(monkeypatch):
    monkeypatch.setenv("CHAT_CLOUD_MODEL", "vendor/model-test")
    monkeypatch.setenv("CHAT_CLOUD_FALLBACK_MODEL", "")
    monkeypatch.setenv("CHAT_CLOUD_TIMEOUT_S", "17")

    settings = MantisSettings(_env_file=None)

    assert settings.CHAT_CLOUD_MODEL == "vendor/model-test"
    assert settings.CHAT_CLOUD_FALLBACK_MODEL == ""
    assert settings.CHAT_CLOUD_TIMEOUT_S == 17


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


def test_fast_and_voice_chain_orders_accept_provider_stage_lists(monkeypatch):
    monkeypatch.setenv("FAST_CHAIN_ORDER", '["anthropic", "openrouter", "chatgpt-plan"]')
    monkeypatch.setenv("VOICE_CHAIN_ORDER", '["chatgpt-plan", "anthropic"]')

    settings = MantisSettings(_env_file=None)

    assert settings.FAST_CHAIN_ORDER == ["anthropic", "openrouter", "chatgpt-plan"]
    assert settings.VOICE_CHAIN_ORDER == ["chatgpt-plan", "anthropic"]


def test_gemini_settings_and_all_chain_orders_can_be_set_from_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-model-from-env")
    monkeypatch.setenv("GEMINI_USD_PER_MTOK_IN", "0.75")
    monkeypatch.setenv("GEMINI_USD_PER_MTOK_OUT", "3.25")
    monkeypatch.setenv("CHAT_AGENT_CHAIN_ORDER", '["gemini", "anthropic"]')
    monkeypatch.setenv("BACKGROUND_CHAIN_ORDER", '["gemini"]')
    monkeypatch.setenv("VISION_CHAIN_ORDER", '["openrouter", "gemini"]')

    settings = MantisSettings(_env_file=None)

    assert settings.GEMINI_API_KEY == "gemini-test-key"
    assert settings.GEMINI_MODEL == "gemini-model-from-env"
    assert settings.GEMINI_USD_PER_MTOK_IN == 0.75
    assert settings.GEMINI_USD_PER_MTOK_OUT == 3.25
    assert settings.CHAT_AGENT_CHAIN_ORDER == ["gemini", "anthropic"]
    assert settings.BACKGROUND_CHAIN_ORDER == ["gemini"]
    assert settings.VISION_CHAIN_ORDER == ["openrouter", "gemini"]


def test_gemini_is_only_added_when_key_and_chain_order_enable_it(monkeypatch):
    import core.backends.factory as factory

    created = _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: False)

    backend = cloud_chain(
        "background",
        cfg=_cfg(
            OPENROUTER_API_KEY="",
            ANTHROPIC_API_KEY="",
            CHAT_CHATGPT_PLAN_ENABLED=False,
            GEMINI_API_KEY="gemini-test-key",
            GEMINI_MODEL="gemini-test-model",
            BACKGROUND_CHAIN_ORDER=["gemini"],
        ),
    )

    assert backend.backend_chain == ("gemini-test-model",)
    assert [(kind, stage.kwargs) for kind, stage in created] == [
        ("gemini", {"api_key": "gemini-test-key", "purpose": "background"}),
    ]


def test_gemini_key_does_not_change_default_chat_chain(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(GEMINI_API_KEY="gemini-test-key"))

    assert "gemini-3-flash" not in backend.backend_chain


def test_gemini_without_key_is_skipped(monkeypatch):
    import core.backends.factory as factory

    _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: False)

    with pytest.raises(CloudChainUnavailableError):
        cloud_chain(
            "vision",
            cfg=_cfg(
                OPENROUTER_API_KEY="",
                ANTHROPIC_API_KEY="",
                CHAT_CHATGPT_PLAN_ENABLED=False,
                GEMINI_API_KEY="",
                VISION_CHAIN_ORDER=["gemini"],
            ),
        )


def test_custom_fast_chain_order_reorders_provider_stages(monkeypatch):
    import core.backends.factory as factory

    created = _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: True)

    backend = cloud_chain(
        "fast",
        reasoning="minimal",
        cfg=_cfg(
            FAST_CHAIN_ORDER=["anthropic", "chatgpt-plan", "openrouter"],
            CHAT_CLOUD_FALLBACK_MODEL="",
        ),
    )

    assert backend.backend_chain == ("claude-haiku-test", "gpt-6-luna", "openai/gpt-6-luna")
    assert [stage.provider_name for stage in backend.backends] == [
        "Claude", "ChatGPT-Abo", "OpenRouter",
    ]
    assert created[0][1].kwargs["reasoning_effort"] == "minimal"


def test_custom_voice_chain_order_reorders_provider_stages(monkeypatch):
    import core.backends.factory as factory

    _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: True)

    backend = cloud_chain(
        "voice",
        cfg=_cfg(
            VOICE_CHAIN_ORDER=["chatgpt-plan", "anthropic", "openrouter"],
            CHAT_CLOUD_FALLBACK_MODEL="",
        ),
    )

    assert backend.backend_chain == ("gpt-6-luna", "claude-haiku-test", "openai/gpt-6-luna")


def test_fast_total_timeout_is_split_across_available_stages(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = cloud_chain(
        "fast",
        total_timeout=6,
        cfg=_cfg(
            FAST_CHAIN_ORDER=["anthropic", "openrouter"],
            CHAT_CLOUD_FALLBACK_MODEL="",
        ),
    )

    assert backend.stage_timeouts == (3, 3)


def test_chat_chain_keeps_openrouter_fallback_and_has_no_local_stage(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg())

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
    )
    assert backend.stage_timeouts == (30, 30, 30)
    assert describe_backend_chain(backend) == (
        "OpenRouter openai/gpt-6-luna → OpenRouter google/gemini-3.1-flash-lite "
        "→ Claude claude-haiku-test"
    )


def test_cloud_chain_routes_purpose_without_local_stage(monkeypatch):
    import core.backends.factory as factory

    created = []

    def openrouter(model, **kwargs):
        backend = FakeBackend("OpenRouter", model)
        backend.kwargs = kwargs
        created.append(("openrouter", backend))
        return backend

    def claude(model, purpose):
        backend = FakeBackend("Anthropic", model)
        backend.kwargs = {"purpose": purpose}
        created.append(("claude", backend))
        return backend

    def chatgpt(model, auth_file, purpose):
        backend = FakeBackend("ChatGPT-Abo", model)
        backend.kwargs = {"auth_file": auth_file, "purpose": purpose}
        created.append(("chatgpt", backend))
        return backend

    monkeypatch.setattr(factory, "OpenRouterBackend", openrouter)
    monkeypatch.setattr(factory, "ClaudeBackend", claude)
    monkeypatch.setattr(factory, "ChatGPTPlanBackend", chatgpt)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: True)

    backend = cloud_chain(
        "voice", cfg=_cfg(CHAT_CLOUD_FALLBACK_MODEL=""),
        reasoning="low", timeout=3,
    )

    assert isinstance(backend, FallbackBackend)
    assert [kind for kind, _ in created] == ["openrouter", "claude", "chatgpt"]
    assert backend.backend_chain == (
        "openai/gpt-6-luna", "claude-haiku-test", "gpt-6-luna",
    )
    assert backend.stage_timeouts == (3, 3, 3)
    assert [backend.kwargs["purpose"] for _, backend in created] == [
        "voice", "voice", "voice",
    ]


def test_full_chain_orders_openrouter_anthropic_chatgpt_then_local(monkeypatch):
    import core.backends.factory as factory

    created = _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: True)

    backend = build_chat_backend(_cfg())

    assert [kind for kind, _ in created] == ["openrouter", "openrouter", "claude", "chatgpt"]
    assert backend.backend_chain == (
        "openai/gpt-6-luna",
        "google/gemini-3.1-flash-lite",
        "claude-haiku-test",
        "gpt-6-luna",
    )
    assert backend.stage_timeouts == (30, 30, 30, 30)
    assert describe_backend_chain(backend) == (
        "OpenRouter openai/gpt-6-luna → OpenRouter google/gemini-3.1-flash-lite "
        "→ Claude claude-haiku-test → ChatGPT-Abo gpt-6-luna"
    )
    assert created[3][1].kwargs == {
        "auth_file": Path("/tmp/mantis-chatgpt-auth-test.json"),
        "purpose": "chat-agent",
    }


def test_local_only_flag_does_not_reintroduce_a_local_llm(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(LLM_LOCAL_ONLY=True))

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == ("openai/gpt-6-luna", "google/gemini-3.1-flash-lite", "claude-haiku-test")
    assert "Ollama" not in describe_backend_chain(backend)


def test_local_only_flag_does_not_disable_background_cloud(monkeypatch):
    _install_fake_backends(monkeypatch)

    provider = build_chat_task_provider(_cfg(
        LLM_LOCAL_ONLY=True,
        BG_CHAT_TASKS_USE_CLOUD=True,
    ))

    assert isinstance(provider, BackendLLMAdapter)
    assert isinstance(provider.backend, FallbackBackend)
    assert provider.backend.backend_chain == (
        "openai/gpt-6-luna", "claude-haiku-test",
    )


def test_missing_openrouter_key_preserves_claude_fallback(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(OPENROUTER_API_KEY=""))

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == ("claude-haiku-test",)
    assert describe_backend_chain(backend) == "Claude claude-haiku-test"


def test_empty_primary_model_disables_openrouter_and_preserves_claude(monkeypatch):
    _install_fake_backends(monkeypatch)

    backend = build_chat_backend(_cfg(CHAT_CLOUD_MODEL=""))

    assert isinstance(backend, FallbackBackend)
    assert backend.backend_chain == ("claude-haiku-test",)


def test_blank_openrouter_key_is_treated_as_missing(monkeypatch):
    created = _install_fake_backends(monkeypatch)

    with pytest.raises(CloudChainUnavailableError, match="Kein Cloud-LLM-Anbieter konfiguriert"):
        build_chat_backend(_cfg(OPENROUTER_API_KEY="  ", ANTHROPIC_API_KEY=""))

    assert created == []


def test_local_only_startup_warning_explains_that_cloud_is_still_used(caplog):
    with caplog.at_level(logging.WARNING, logger="core.backends.factory"):
        warn_if_local_only(_cfg(LLM_LOCAL_ONLY=True))

    assert "lokale LLM-Modelle sind entfernt" in caplog.text
    assert "Cloud-Kette" in caplog.text


def test_chat_task_provider_ignores_removed_local_privacy_switch(monkeypatch):
    _install_fake_backends(monkeypatch)

    provider = build_chat_task_provider(_cfg(BG_CHAT_TASKS_USE_CLOUD=True))
    disabled = build_chat_task_provider(_cfg(BG_CHAT_TASKS_USE_CLOUD=False))

    assert provider is not None
    assert provider.backend.backend_chain == (
        "openai/gpt-6-luna",
        "claude-haiku-test",
    )
    assert [stage.purpose for stage in provider.backend.backends] == [
        "background",
        "background",
    ]
    assert isinstance(disabled, BackendLLMAdapter)
    assert disabled.backend.backend_chain == provider.backend.backend_chain


def test_background_chat_chain_includes_anthropic_and_chatgpt_in_order(monkeypatch):
    import core.backends.factory as factory

    _install_fake_backends(monkeypatch)
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda cfg: True)

    provider = build_chat_task_provider(_cfg())

    assert provider.backend.backend_chain == (
        "openai/gpt-6-luna",
        "claude-haiku-test",
        "gpt-6-luna",
    )
    assert [stage.kwargs.get("purpose", "chat-agent") for stage in provider.backend.backends] == [
        "background", "background", "background",
    ]


def test_non_chat_purposes_do_not_inherit_optional_text_chat_fallback(monkeypatch):
    _install_fake_backends(monkeypatch)
    import core.backends.factory as factory
    monkeypatch.setattr(factory, "chatgpt_auth_is_configured", lambda _cfg: True)

    backend = cloud_chain("vision", cfg=_cfg())

    assert backend.backend_chain == (
        "openai/gpt-6-luna", "claude-haiku-test", "gpt-6-luna",
    )
    chat = build_chat_backend(_cfg())
    assert chat.backend_chain[:2] == (
        "openai/gpt-6-luna", "google/gemini-3.1-flash-lite",
    )


def test_missing_chatgpt_login_is_reported_and_stage_is_skipped(monkeypatch, caplog):
    _install_fake_backends(monkeypatch)
    caplog.set_level("INFO")

    backend = build_chat_backend(_cfg())

    assert backend.backend_chain == (
        "openai/gpt-6-luna", "google/gemini-3.1-flash-lite", "claude-haiku-test",
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
    with pytest.raises(CloudChainUnavailableError, match="Kein Cloud-LLM-Anbieter konfiguriert"):
        build_chat_backend(_cfg(
            OPENROUTER_API_KEY="", ANTHROPIC_API_KEY="", CHATGPT_AUTH_FILE=str(path),
        ))


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

    assert backend.backend_chain == ("openai/gpt-6-luna", "claude-haiku-test")
    assert backend.stage_timeouts == (30, 30)
