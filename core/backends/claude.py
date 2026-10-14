"""
Claude-Backend für den Agent-Loop.
Übersetzt das einheitliche Backend-Interface in Anthropic-API-Calls mit Tool-Use.
"""
import json
import logging

import anthropic

from core.backends.base import AgentBackend, StreamCb
from core.anthropic_models import ensure_anthropic_model_allowed
import config

log = logging.getLogger(__name__)

_TEMPERATURE_UNSUPPORTED_MODEL_PREFIXES = ("claude-haiku-5-5",)


def _supports_temperature(model: str) -> bool:
    """Gibt die Temperatur nur an Modellfamilien weiter, die sie akzeptieren."""
    normalized = model.casefold().strip()
    return not normalized.startswith(_TEMPERATURE_UNSUPPORTED_MODEL_PREFIXES)


def _ollama_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Konvertiert Ollama/OpenAI Tool-Schema → Anthropic Tool-Schema."""
    result = []
    for t in tools:
        # Ollama-Format: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        fn = t.get("function", t)
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _ollama_msgs_to_anthropic(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """
    Trennt System-Prompt ab und konvertiert Messages ins Anthropic-Format.
    Tool-Results werden korrekt als tool_result-Blöcke eingebettet.
    Gibt (system, messages) zurück.
    """
    system = None
    result = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")

        if role == "system":
            system = m.get("content", "")
            i += 1
            continue

        if role == "tool":
            # Tool-Results gehören als user-Nachricht mit tool_result-Block
            tool_use_id = m.get("tool_use_id") or f"tool_{i}"
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": str(m.get("content", "")),
                }]
            })
            i += 1
            continue

        if role == "assistant":
            content_parts = []
            text = m.get("content", "")
            if isinstance(text, list):
                content_parts.extend(_content_to_anthropic(text))
            elif text:
                content_parts.append({"type": "text", "text": text})
            # Tool-Calls aus Ollama-Format → Anthropic tool_use-Blöcke
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                content_parts.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{len(result)}",
                    "name": fn.get("name", ""),
                    "input": args,
                })
            result.append({"role": "assistant", "content": content_parts or text})
            i += 1
            continue

        # user
        result.append({"role": "user", "content": _content_to_anthropic(m.get("content", ""))})
        i += 1

    return system, result


def _extract_tool_calls(response, tools: list[dict] | None = None) -> list[dict]:
    """Extrahiert Tool-Calls aus Anthropic-Response ins einheitliche Format."""
    required_by_name = {}
    for tool in tools or []:
        function = tool.get("function") or tool
        parameters = function.get("parameters") or {}
        required_by_name[function.get("name", "")] = parameters.get("required") or []

    calls = []
    for block in response.content:
        if block.type == "tool_use":
            if not isinstance(block.input, dict):
                raise ValueError(f"Anthropic Tool-Argumente für {block.name} müssen ein JSON-Objekt sein")
            missing = [name for name in required_by_name.get(block.name, []) if name not in block.input]
            if missing:
                raise ValueError(
                    f"Anthropic Tool-Argumente fehlen für Pflichtargumente: {', '.join(missing)}",
                )
            calls.append({
                "id": block.id,
                "function": {
                    "name": block.name,
                    "arguments": block.input or {},
                },
            })
    return calls


def _extract_text(response) -> str:
    parts = [b.text for b in response.content if b.type == "text"]
    return "".join(parts)


def _content_to_anthropic(content):
    if not isinstance(content, list):
        return content
    result = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            result.append({"type": "text", "text": str(block.get("text", ""))})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64" and source.get("data"):
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": source.get("media_type", "image/jpeg"),
                        "data": source["data"],
                    },
                })
    return result


class ClaudeBackend(AgentBackend):
    provider_name = "Anthropic"

    def __init__(self, model: str | None = None, purpose: str = "chat-agent"):
        self._model = model or config.CLAUDE_CHAT_MODEL
        ensure_anthropic_model_allowed(self._model)
        self._purpose = purpose
        self._client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_vision(self) -> bool:
        return "haiku" in self._model.casefold()

    async def call(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
        temperature: float,
        max_tokens: int,
        think: bool = False,
        force_tool_call: bool = False,
    ) -> tuple[str, list[dict]]:
        ensure_anthropic_model_allowed(self._model)
        system, anthropic_msgs = _ollama_msgs_to_anthropic(messages)

        kwargs: dict = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=anthropic_msgs,
        )
        if _supports_temperature(self._model):
            kwargs["temperature"] = temperature
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _ollama_tools_to_anthropic(tools)
        if force_tool_call and tools:
            kwargs["tool_choice"] = {"type": "any"}

        if stream_cb:
            collected = ""
            received_message_stop = False
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "message_stop":
                        received_message_stop = True
                    elif (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        collected += event.delta.text
                        try:
                            await stream_cb(collected)
                        except Exception:
                            pass
            final = await stream.get_final_message()
            if not received_message_stop:
                raise RuntimeError("Anthropic-Stream endete ohne message_stop")
            stop_reason = getattr(final, "stop_reason", None)
            if not isinstance(stop_reason, str) or not stop_reason:
                raise RuntimeError("Anthropic-Stream endete ohne Abschluss")
            self._record_usage(final)
            tool_calls = _extract_tool_calls(final, tools)
            return _extract_text(final) or collected, tool_calls

        response = await self._client.messages.create(**kwargs)
        self._record_usage(response)
        tool_calls = _extract_tool_calls(response, tools)

        # Tool-Result-Messages brauchen tool_use_id → rückwärts befüllen
        # (wird beim nächsten Call aus dem trace rekonstruiert)
        for tc in tool_calls:
            tc["tool_use_id"] = tc["id"]

        return _extract_text(response), tool_calls

    def _record_usage(self, response) -> None:
        try:
            from core import llm_usage
            u = getattr(response, "usage", None)
            if u:
                llm_usage.record("claude", self._model,
                                 getattr(u, "input_tokens", 0) or 0,
                                 getattr(u, "output_tokens", 0) or 0,
                             purpose=self._purpose)
        except Exception:
            pass

    async def warmup(self) -> None:
        log.info(f"☁️  Claude-Backend bereit ({self._model})")
