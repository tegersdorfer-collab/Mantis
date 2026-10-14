"""Ollama-Laufzeit bleibt auf lokale Embeddings begrenzt."""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import config
from settings import MantisSettings


def _imports_ollama(tree: ast.Module) -> bool:
    return any(
        (isinstance(node, ast.Import) and any(alias.name == "ollama" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "ollama")
        for node in ast.walk(tree)
    )


def test_num_ctx_for_uses_default_and_exact_model_override():
    assert config.OLLAMA_NUM_CTX == 8192
    assert config.num_ctx_for("qwen3-embedding:0.6b") == 8192
    assert config.num_ctx_for("some-other-model") == 8192


def test_num_ctx_overrides_parse_whitespace_and_empty_entries():
    settings = MantisSettings(
        _env_file=None,
        OLLAMA_NUM_CTX_OVERRIDES=" qwen3-embedding:0.6b = 4096, , other=8192 ",
    )
    assert settings.OLLAMA_NUM_CTX_OVERRIDES == "qwen3-embedding:0.6b=4096,other=8192"
    assert MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES="").OLLAMA_NUM_CTX_OVERRIDES == ""
    assert MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES="  ,  ").OLLAMA_NUM_CTX_OVERRIDES == ""


@pytest.mark.parametrize(
    "value",
    ["qwen3-embedding:0.6b", "=8192", "q=", "q=lots", "q=0", "q=8192,q=4096", "q=8192=4096"],
)
def test_invalid_num_ctx_override_fails_settings_validation(value):
    with pytest.raises(ValidationError):
        MantisSettings(_env_file=None, OLLAMA_NUM_CTX_OVERRIDES=value)


def test_ollama_imports_and_calls_are_embedding_only():
    root = Path(__file__).resolve().parents[1]
    excluded = {"tests", "bench", ".worktrees", ".git", ".venv", "venv", "node_modules"}
    failures = []
    ollama_modules = []

    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if not _imports_ollama(tree):
                continue
            relative = path.relative_to(root).as_posix()
            ollama_modules.append(relative)
            if relative != "llm/local.py":
                failures.append(f"Ollama-Import außerhalb des Embedding-Providers: {relative}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    receiver = ast.unparse(node.func.value)
                    if receiver in {"self._client", "self._client"} and node.func.attr not in {
                        "embed", "list", "pull",
                    }:
                        failures.append(f"verbotener Ollama-Aufruf {node.func.attr} in {relative}:{node.lineno}")

    assert ollama_modules == ["llm/local.py"]
    assert not failures, "\n".join(failures)


def test_legacy_local_generation_settings_are_removed():
    settings = MantisSettings(_env_file=None)
    for name in (
        "OLLAMA_MODEL", "AGENT_MODEL_FAST", "AGENT_MODEL_STRONG", "ADDRESS_CHECK_MODEL",
        "VOICE_AGENT_MODEL", "VOICE_AGENT_KEEP_ALIVE", "BG_DEFAULT_MODEL", "BG_REASONING_MODEL",
        "BG_CODE_MODEL", "VISION_MODEL",
    ):
        assert not hasattr(settings, name), name
    assert settings.LZG_EMBED_MODEL == "qwen3-embedding:0.6b"
