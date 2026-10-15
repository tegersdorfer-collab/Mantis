"""Vertex-AI Gemini-Backend für das gemeinsame Agent-Backend-Interface."""
import json
import logging
import uuid
from urllib.parse import quote

import httpx

import config
from core.backends.base import AgentBackend, StreamCb
from core.backends.fallback import COOLDOWN_S
from core.error_redaction import redact_error_text

log = logging.getLogger(__name__)

BASE_URL = "https://aiplatform.googleapis.com"
TIMEOUT_S = config.CHAT_CLOUD_TIMEOUT_S


class GeminiAPIError(RuntimeError):
    """Vertex-AI-Fehler mit Statuscode für die Cloud-Fallback-Kette."""

    def __init__(self, message: str, *, status_code: int, cooldown_s: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        if cooldown_s is not None:
            self.fallback_cooldown_s = cooldown_s


def _text_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []

    result = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", ""))
            if text:
                result.append({"text": text})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64" and source.get("data"):
                result.append({"inlineData": {
                    "mimeType": source.get("media_type", "image/jpeg"),
                    "data": source["data"],
                }})
    return result


def _append_content(contents: list[dict], role: str, parts: list[dict]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _history_to_gemini(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Trennt System-Text ab und konvertiert Agent-Verlauf und Tool-Rückgaben."""
    system_parts: list[str] = []
    contents: list[dict] = []
    pending_names: dict[str, str] = {}
    pending_ids: list[str] = []

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            system_parts.extend(
                part["text"] for part in _text_parts(message.get("content", ""))
                if part.get("text")
            )
            continue

        if role == "tool":
            call_id = message.get("tool_use_id") or message.get("tool_call_id") or ""
            name = message.get("name") or pending_names.get(call_id, "")
            if not call_id and pending_ids:
                call_id = pending_ids[0]
                name = name or pending_names.get(call_id, "")
            if not name:
                raise ValueError("Gemini Tool-Ergebnis hat keinen passenden Funktionsnamen")
            if call_id in pending_names:
                pending_names.pop(call_id, None)
                if call_id in pending_ids:
                    pending_ids.remove(call_id)
            response = {"name": name, "response": {"result": str(message.get("content", ""))}}
            if call_id:
                response["id"] = call_id
            _append_content(contents, "user", [{"functionResponse": response}])
            continue

        if role == "assistant":
            parts = _text_parts(message.get("content", ""))
            for call_index, tool_call in enumerate(message.get("tool_calls") or []):
                function = tool_call.get("function") or {}
                name = str(function.get("name", ""))
                if not name:
                    raise ValueError("Gemini Tool-Aufruf hat keinen Funktionsnamen")
                call_id = str(
                    tool_call.get("id") or tool_call.get("tool_use_id")
                    or f"call_{index}_{call_index}_{uuid.uuid4().hex[:8]}"
                )
                args = function.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except (TypeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                parts.append({"functionCall": {"id": call_id, "name": name, "args": args}})
                pending_names[call_id] = name
                pending_ids.append(call_id)
            _append_content(contents, "model", parts)
            continue

        if role == "user":
            _append_content(contents, "user", _text_parts(message.get("content", "")))

    system = "\n".join(text for text in system_parts if text.strip())
    return (system or None), contents


def _function_declarations(tools: list[dict] | None) -> list[dict]:
    declarations = []
    for tool in tools or []:
        function = tool.get("function") or tool
        name = str(function.get("name", ""))
        if not name:
            raise ValueError("Gemini Tool-Schema hat keinen Funktionsnamen")
        declaration = {
            "name": name,
            "parameters": function.get("parameters") or {
                "type": "object", "properties": {},
            },
        }
        if function.get("description"):
            declaration["description"] = function["description"]
        declarations.append(declaration)
    return declarations


def _required_arguments(tools: list[dict] | None) -> dict[str, list[str]]:
    result = {}
    for tool in tools or []:
        function = tool.get("function") or tool
        name = function.get("name", "")
        parameters = function.get("parameters") or {}
        result[name] = parameters.get("required") or []
    return result


def _normalize_function_call(function_call: dict, required: dict[str, list[str]]) -> dict:
    name = str(function_call.get("name", ""))
    if not name:
        raise ValueError("Gemini Tool-Aufruf hat keinen Funktionsnamen")
    args = function_call.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (TypeError, ValueError) as exc:
            raise ValueError("Gemini Tool-Argumente enthalten ungültiges JSON") from exc
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("Gemini Tool-Argumente müssen ein JSON-Objekt sein")
    missing = [key for key in required.get(name, []) if key not in args]
    if missing:
        raise ValueError(
            f"Gemini Tool-Argumente fehlen für Pflichtargumente: {', '.join(missing)}"
        )
    call_id = str(function_call.get("id") or uuid.uuid4().hex)
    return {
        "id": call_id,
        "tool_use_id": call_id,
        "function": {"name": name, "arguments": args},
    }


def _extract_response(data: dict, tools: list[dict] | None) -> tuple[str, list[dict]]:
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    content = candidate.get("content") or {}
    text_parts = []
    calls = []
    required = _required_arguments(tools)
    for part in content.get("parts") or []:
        if isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            calls.append(_normalize_function_call(function_call, required))
    return "".join(text_parts), calls


class GeminiBackend(AgentBackend):
    """Direkter Vertex AI Express-Mode-Aufruf mit API-Key im Header."""

    provider_name = "Gemini"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        purpose: str = "chat-agent",
    ):
        self._model = str(model if model is not None else config.GEMINI_MODEL).strip()
        key = str(api_key if api_key is not None else config.GEMINI_API_KEY).strip()
        if not key:
            raise ValueError("GeminiBackend braucht GEMINI_API_KEY")
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("Gemini API-Key ist ungültig")
        if not self._model:
            raise ValueError("GeminiBackend braucht GEMINI_MODEL")
        self._api_key = key
        self._purpose = purpose
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_S)
        self._headers = {"x-goog-api-key": key}

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_vision(self) -> bool:
        return True

    def _url(self, *, streaming: bool) -> str:
        model = quote(self._model, safe="-_.")
        method = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        return f"/v1/publishers/google/models/{model}:{method}"

    def _payload(self, messages, tools, temperature, max_tokens, force_tool_call) -> dict:
        system, contents = _history_to_gemini(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        declarations = _function_declarations(tools)
        if declarations:
            payload["tools"] = [{"functionDeclarations": declarations}]
            if force_tool_call:
                payload["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY"},
                }
        return payload

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
        payload = self._payload(messages, tools, temperature, max_tokens, force_tool_call)
        if stream_cb is not None:
            return await self._call_streaming(payload, tools, stream_cb)

        response = await self._client.post(
            self._url(streaming=False), json=payload, headers=self._headers,
        )
        if response.is_error:
            self._raise_api_error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Gemini-Antwort hat ein ungültiges Format")
        if data.get("error"):
            self._raise_api_error(response, data)
        self._record_usage(data.get("usageMetadata"))
        return _extract_response(data, tools)

    async def _call_streaming(
        self, payload: dict, tools: list[dict] | None, stream_cb: StreamCb,
    ) -> tuple[str, list[dict]]:
        parts: list[str] = []
        calls_by_index: dict[tuple[int, int], dict] = {}
        usage = None
        completed = False

        try:
            async with self._client.stream(
                "POST", self._url(streaming=True), json=payload, headers=self._headers,
            ) as response:
                if response.is_error:
                    await response.aread()
                    self._raise_api_error(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    if raw == "[DONE]":
                        completed = True
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError as exc:
                        raise RuntimeError("Gemini-Stream enthält ungültiges JSON") from exc
                    if not isinstance(event, dict):
                        raise RuntimeError("Gemini-Stream enthält ein ungültiges Ereignis")
                    if event.get("error"):
                        self._raise_api_error(response, event)
                    if event.get("usageMetadata") is not None:
                        if not isinstance(event["usageMetadata"], dict):
                            raise RuntimeError("Gemini-Stream enthält ungültige Usage-Daten")
                        usage = event["usageMetadata"]

                    candidates = event.get("candidates") or []
                    if not isinstance(candidates, list):
                        raise RuntimeError("Gemini-Stream enthält ungültige Candidates")
                    for candidate_index, candidate in enumerate(candidates):
                        if not isinstance(candidate, dict):
                            raise RuntimeError("Gemini-Stream enthält eine ungültige Candidate")
                        content = candidate.get("content") or {}
                        candidate_index = candidate.get("index", candidate_index)
                        for part_index, part in enumerate(content.get("parts") or []):
                            if not isinstance(part, dict):
                                continue
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                parts.append(text)
                                try:
                                    await stream_cb(text)
                                except Exception:
                                    pass
                            function_call = part.get("functionCall")
                            if isinstance(function_call, dict):
                                key = (candidate_index, part_index)
                                slot = calls_by_index.setdefault(key, {"args": {}})
                                for field in ("id", "name"):
                                    if function_call.get(field):
                                        slot[field] = function_call[field]
                                args = function_call.get("args")
                                if isinstance(args, dict):
                                    slot["args"].update(args)
                        if candidate.get("finishReason") is not None:
                            completed = True

            if not completed:
                raise RuntimeError("Gemini-Stream endete ohne Abschluss")
            required = _required_arguments(tools)
            calls = [
                _normalize_function_call(slot, required)
                for _, slot in sorted(calls_by_index.items())
            ]
        except BaseException:
            self._record_usage(usage)
            raise

        self._record_usage(usage)
        return "".join(parts), calls

    def _raise_api_error(self, response: httpx.Response, body: dict | None = None) -> None:
        data = body
        if data is None:
            try:
                data = response.json()
            except (ValueError, httpx.ResponseNotRead):
                data = {}
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        raw_status = error.get("code")
        try:
            status_code = int(raw_status) if raw_status is not None else response.status_code
        except (TypeError, ValueError):
            status_code = response.status_code
        status = str(error.get("status") or "").strip()
        detail = str(error.get("message") or "").strip()
        if not detail:
            detail = f"HTTP {response.status_code}"
        detail = detail.replace(self._api_key, "[REDACTED]")
        detail = redact_error_text(detail)
        quota_text = f"{status} {detail}".casefold()
        quota_error = (
            response.status_code == 429 or status_code == 429
            or "resource_exhausted" in quota_text
            or "quota" in quota_text
        )
        cooldown = COOLDOWN_S if quota_error else None
        status_detail = f" {status}" if status else ""
        raise GeminiAPIError(
            f"Vertex AI Gemini API-Fehler {status_code}{status_detail}: {detail}",
            status_code=status_code,
            cooldown_s=cooldown,
        )

    def _record_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        try:
            input_tokens = int(usage.get("promptTokenCount") or 0)
            output_tokens = int(
                usage.get("candidatesTokenCount")
                or usage.get("responseTokenCount") or 0
            )
            input_price = float(config.GEMINI_USD_PER_MTOK_IN)
            output_price = float(config.GEMINI_USD_PER_MTOK_OUT)
            actual_cost = (
                input_tokens * input_price + output_tokens * output_price
            ) / 1_000_000
            from core import llm_usage

            llm_usage.record(
                "gemini", self._model, input_tokens, output_tokens,
                purpose=self._purpose, actual_cost=actual_cost,
            )
        except Exception:
            log.debug("Gemini-Usage konnte nicht erfasst werden", exc_info=True)

    async def warmup(self) -> None:
        log.info("Vertex-AI-Gemini-Backend bereit (%s)", self._model)

