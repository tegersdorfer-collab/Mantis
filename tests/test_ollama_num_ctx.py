"""Regressionstests für modellbezogene num_ctx-Werte an Ollama-Aufrufen.

Der AST-Guard betrachtet in Produktionsdateien mit einem `import ollama` jeden
`.chat()`/`.generate()`-Aufruf und direkte `_client.embed()`-Aufrufe. Ein Call
muss `options={..., "num_ctx": ...}` enthalten oder `options` muss direkt aus
`config.ollama_options(...)` kommen. Provider-Aufrufe wie `self._llm.embed()`
sind keine direkten AsyncClient-Calls und werden vom Embed-Guard ausgelassen.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from settings import MantisSettings


class FakeAsyncClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def chat(self, **kwargs):
        self.calls.append(("chat", kwargs))
        if kwargs.get("stream"):
            async def chunks():
                yield SimpleNamespace(message=SimpleNamespace(content="Antwort"))

            return chunks()
        return SimpleNamespace(
            message=SimpleNamespace(content="Antwort"),
            prompt_eval_count=0,
            eval_count=0,
        )

    async def embed(self, **kwargs):
        self.calls.append(("embed", kwargs))
        inputs = kwargs["input"]
        count = len(inputs) if isinstance(inputs, list) else 1
        return SimpleNamespace(embeddings=[[float(i)] for i in range(count)])

    async def ps(self):
        return SimpleNamespace(models=[SimpleNamespace(name="other-model:7b")])


def _run(coro):
    return asyncio.run(coro)


def test_num_ctx_for_uses_default_and_exact_model_override():
    assert config.OLLAMA_NUM_CTX == 8192
    assert config.num_ctx_for("qwen3.5:9b") == 8192
    assert config.num_ctx_for("gemma4:e2b") == 16384
    assert config.num_ctx_for("gemma4:e2b-instruct") == 8192


def test_num_ctx_overrides_parse_whitespace_and_empty_entries():
    settings = MantisSettings(
        _env_file=None,
        OLLAMA_NUM_CTX_OVERRIDES=" gemma4:e2b = 16384, , qwen3.5:9b= 4096 ",
    )
    assert settings.OLLAMA_NUM_CTX_OVERRIDES == "gemma4:e2b=16384,qwen3.5:9b=4096"
    assert MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES="").OLLAMA_NUM_CTX_OVERRIDES == ""
    assert MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES="  ,  ").OLLAMA_NUM_CTX_OVERRIDES == ""


@pytest.mark.parametrize(
    "value",
    ["gemma4:e2b", "=8192", "gemma4:e2b=", "gemma4:e2b=lots", "gemma4:e2b=0",
     "q=8192,q=4096", "qwen=8192=4096"],
)
def test_invalid_num_ctx_override_fails_settings_validation(value):
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES=value)


def test_fast_ask_and_warmup_pass_context_for_their_models(monkeypatch):
    from core import fast

    client = FakeAsyncClient()
    monkeypatch.setattr(fast, "_client", client)

    assert _run(fast.ask("kurz", model="gemma4:e2b")) == "Antwort"
    _run(fast.warmup())

    ask_call, warmup_call = [kwargs for method, kwargs in client.calls if method == "chat"]
    assert ask_call["model"] == "gemma4:e2b"
    assert ask_call["options"]["num_ctx"] == config.num_ctx_for("gemma4:e2b")
    assert warmup_call["model"] == config.AGENT_MODEL_FAST
    assert warmup_call["options"]["num_ctx"] == config.num_ctx_for(config.AGENT_MODEL_FAST)


def test_ollama_provider_chat_stream_and_embeddings_pass_model_context(monkeypatch):
    from llm.local import OllamaProvider

    client = FakeAsyncClient()
    monkeypatch.setattr("llm.local._ollama.AsyncClient", lambda **kwargs: client)
    provider = OllamaProvider(model="gemma4:e2b", embed_model="embed:0.6b")

    assert _run(provider.chat([{"role": "user", "content": "hallo"}])) == "Antwort"

    async def collect_stream():
        return [part async for part in provider.stream([{"role": "user", "content": "hallo"}])]

    assert _run(collect_stream()) == ["Antwort"]
    assert _run(provider.embed("einzelner text")) == [0.0]
    assert _run(provider.embed_batch(["text zwei", "text drei"])) == [[0.0], [1.0]]

    chat_calls = [kwargs for method, kwargs in client.calls if method == "chat"]
    embed_calls = [kwargs for method, kwargs in client.calls if method == "embed"]
    assert len(chat_calls) == 2
    assert all(call["options"]["num_ctx"] == config.num_ctx_for("gemma4:e2b") for call in chat_calls)
    assert len(embed_calls) == 2
    assert all(call["options"]["num_ctx"] == config.num_ctx_for("embed:0.6b") for call in embed_calls)


def test_unload_others_uses_the_unloaded_model_context(monkeypatch):
    from llm.local import OllamaProvider

    client = FakeAsyncClient()
    monkeypatch.setattr("llm.local._ollama.AsyncClient", lambda **kwargs: client)
    provider = OllamaProvider(model="gemma4:e2b")

    _run(provider.unload_others())

    call = client.calls[0][1]
    assert call["model"] == "other-model:7b"
    assert call["options"]["num_ctx"] == config.num_ctx_for("other-model:7b")


def test_ollama_backend_stream_nonstream_and_warmup_pass_same_context(monkeypatch):
    from core.backends.ollama import OllamaBackend

    client = FakeAsyncClient()
    monkeypatch.setattr("core.backends.ollama._ollama.AsyncClient", lambda **kwargs: client)
    backend = OllamaBackend(model="gemma4:e2b")

    args = dict(messages=[{"role": "user", "content": "hallo"}], tools=None,
                temperature=0.1, max_tokens=10)
    assert _run(backend.call(**args, stream_cb=None)) == ("Antwort", [])
    streamed: list[str] = []

    async def on_stream(text):
        streamed.append(text)

    assert _run(backend.call(**args, stream_cb=on_stream)) == ("Antwort", [])
    _run(backend.warmup())

    chat_calls = [kwargs for method, kwargs in client.calls if method == "chat"]
    assert len(chat_calls) == 3
    assert all(call["options"]["num_ctx"] == config.num_ctx_for("gemma4:e2b") for call in chat_calls)
    assert [call.get("stream", False) for call in chat_calls] == [False, True, False]
    assert streamed == ["Antwort"]


def test_vision_passes_context_for_selected_model(monkeypatch):
    import ollama
    from core import vision

    client = FakeAsyncClient()
    monkeypatch.setattr(ollama, "AsyncClient", lambda **kwargs: client)

    assert _run(vision.describe_image(b"fake image", "Beschreibe es", model="gemma4:e2b")) == "Antwort"
    call = client.calls[0][1]
    assert call["model"] == "gemma4:e2b"
    assert call["options"]["num_ctx"] == config.num_ctx_for("gemma4:e2b")


def test_memory_conflict_judge_passes_context_for_selected_model():
    from memory.conflict import make_llm_judge

    client = FakeAsyncClient()
    judge = make_llm_judge(client, "gemma4:e2b")
    assert _run(judge("alter Fakt", "neuer Fakt")) is False
    call = client.calls[0][1]
    assert call["options"]["num_ctx"] == config.num_ctx_for("gemma4:e2b")


def _dotted_name(node: ast.AST) -> str:
    return ast.unparse(node) if node is not None else ""


def _has_num_ctx_options(call: ast.Call, helper_option_names: set[str]) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "options":
            continue
        value = keyword.value
        if isinstance(value, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == "num_ctx" for key in value.keys
        ):
            return True
        if isinstance(value, ast.Call) and _dotted_name(value.func).endswith("ollama_options"):
            return True
        if isinstance(value, ast.Name) and value.id in helper_option_names:
            return True
    return False


def _imports_ollama(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name == "ollama" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "ollama":
            return True
    return False


def test_production_ollama_calls_always_set_num_ctx():
    """Direkte Ollama-Calls in Ollama-importierenden Produktionsdateien müssen num_ctx setzen.

    Der AST-Regel zufolge steht `num_ctx` direkt im options-Dict, oder der
    options-Wert kommt aus `config.ollama_options(...)` (direkt bzw. per Variable).
    """
    root = Path(__file__).resolve().parents[1]
    excluded = {"tests", "bench", "scripts", ".worktrees", "mlx_models", ".git", ".venv", "venv", "node_modules"}
    failures: list[str] = []
    checked: list[str] = []

    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if not _imports_ollama(tree):
                continue

            helper_option_names = {
                target.id
                for node in ast.walk(tree)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Call)
                and _dotted_name(node.value.func).endswith("ollama_options")
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                method = node.func.attr
                if method not in {"chat", "generate", "embed"}:
                    continue
                receiver = _dotted_name(node.func.value)
                # Provider-facing embeddings (for example self._llm.embed) do not
                # call Ollama's AsyncClient directly.
                if method == "embed" and not (
                    receiver in {"_client", "client"} or receiver.endswith("._client")
                ):
                    continue
                location = f"{path.relative_to(root)}:{node.lineno}"
                checked.append(location)
                if not _has_num_ctx_options(node, helper_option_names):
                    failures.append(location)

    assert checked, "AST-Guard hat keine Ollama-Aufrufe geprüft"
    assert not failures, "Ollama-Aufrufe ohne options.num_ctx: " + ", ".join(failures)
