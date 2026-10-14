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


def _ollama_generation_violations(tree: ast.Module, relative: str) -> list[str]:
    ollama_base_names = {"OLLAMA_BASE_URL"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            ollama_base_names.update(
                alias.asname or alias.name
                for alias in node.names if alias.name == "OLLAMA_BASE_URL"
            )

    def references_ollama_base(expression):
        return any(
            (isinstance(part, ast.Attribute) and part.attr == "OLLAMA_BASE_URL")
            or (isinstance(part, ast.Name) and part.id in ollama_base_names)
            or (isinstance(part, ast.Constant) and "11434" in str(part.value))
            for part in ast.walk(expression)
        )

    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and references_ollama_base(node.value):
                ollama_base_names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )

    ollama_client_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        factory_name = getattr(node.value.func, "attr", "")
        base_url = next((keyword.value for keyword in node.value.keywords if keyword.arg == "base_url"), None)
        if factory_name.endswith("Client") and base_url is not None and references_ollama_base(base_url):
            ollama_client_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr.casefold() not in {"post", "put", "patch", "request", "urlopen"}:
            continue
        url_parts = [keyword.value for keyword in node.keywords if keyword.arg in {"url", "uri"}]
        url_parts.extend(node.args[:2])
        url_text = " ".join(ast.unparse(part) for part in url_parts)
        path = next((suffix for suffix in ("/api/chat", "/api/generate") if suffix in url_text), None)
        if path is None:
            continue
        receiver = node.func.value
        receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
        has_ollama_base = (
            any(references_ollama_base(part) for part in url_parts)
            or receiver_name in ollama_client_names
        )
        if has_ollama_base:
            violations.append(f"verbotener roher Ollama-Generierungsaufruf {path} in {relative}:{node.lineno}")
    return violations


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
            relative = path.relative_to(root).as_posix()
            if _imports_ollama(tree):
                ollama_modules.append(relative)
                if relative != "llm/local.py":
                    failures.append(f"Ollama-Import außerhalb des Embedding-Providers: {relative}")
                else:
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                            receiver = ast.unparse(node.func.value)
                            if receiver == "self._client" and node.func.attr not in {"embed", "list", "pull"}:
                                failures.append(
                                    f"verbotener Ollama-Aufruf {node.func.attr} in {relative}:{node.lineno}"
                                )
            failures.extend(_ollama_generation_violations(tree, relative))

    assert ollama_modules == ["llm/local.py"]
    assert not failures, "\n".join(failures)


def test_raw_http_ollama_chat_and_generate_mutations_are_rejected():
    mutant = ast.parse(
        """
import httpx
import config
from config import OLLAMA_BASE_URL as ollama_base
async def chat(client, payload):
    await client.post(f\"{config.OLLAMA_BASE_URL}/api/chat\", json=payload)
async def generate(client, payload):
    await client.request(\"POST\", config.OLLAMA_BASE_URL + \"/api/generate\", json=payload)
async def alias_chat(payload):
    await httpx.post(f\"{ollama_base}/api/chat\", json=payload)
async def session_generate(payload):
    client = httpx.AsyncClient(base_url=ollama_base)
    await client.post(\"/api/generate\", json=payload)
"""
    )

    violations = _ollama_generation_violations(mutant, "mutation.py")

    assert len(violations) == 4
    assert sum("/api/chat" in violation for violation in violations) == 2
    assert sum("/api/generate" in violation for violation in violations) == 2


def test_legacy_local_generation_settings_are_removed():
    settings = MantisSettings(_env_file=None)
    for name in (
        "OLLAMA_MODEL", "AGENT_MODEL_FAST", "AGENT_MODEL_STRONG", "ADDRESS_CHECK_MODEL",
        "VOICE_AGENT_MODEL", "VOICE_AGENT_KEEP_ALIVE", "BG_DEFAULT_MODEL", "BG_REASONING_MODEL",
        "BG_CODE_MODEL", "VISION_MODEL",
    ):
        assert not hasattr(settings, name), name
    assert settings.LZG_EMBED_MODEL == "qwen3-embedding:0.6b"
