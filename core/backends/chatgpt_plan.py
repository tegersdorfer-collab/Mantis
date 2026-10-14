"""ChatGPT-Abo-Backend über die Responses-API mit lokal gespeicherten OAuth-Tokens."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path

import httpx

from core.auth.chatgpt import RESOURCE, TOKEN_URL, load_auth, save_auth
from core.backends.base import AgentBackend, StreamCb
from core.error_redaction import redact_error_text

log = logging.getLogger(__name__)

RESPONSES_URL = f"{RESOURCE}/responses"
REFRESH_SKEW_S = 300
QUOTA_PAUSE_S = 30 * 60
_refresh_lock = asyncio.Lock()
_KNOWN_CODES = {
    "subscription_sharing_usage_unavailable",
    "subscription_sharing_user_unavailable",
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_invalid_user",
    "subscription_sharing_user_not_eligible",
    "subscription_sharing_route_not_supported",
    "subscription_sharing_unsupported_capability",
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
    "invalid_client",
}
_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])(?:" + "|".join(
    re.escape(code) for code in sorted(_KNOWN_CODES, key=len, reverse=True)
) + r"|chatpass_v2_[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])")


class ChatGPTPlanError(RuntimeError):
    """Sanitisierter Providerfehler samt Failover-Politik für die ChatGPT-Stufe."""

    def __init__(
        self,
        message: str,
        *,
        cooldown_s: float | None = None,
        disable_backend: bool = False,
        requires_login: bool = False,
        error_code: str = "",
        param: str = "",
    ):
        super().__init__(message)
        self.fallback_cooldown_s = cooldown_s
        self.cooldown_s = cooldown_s
        self.disable_backend = disable_backend
        self.requires_login = requires_login
        self.error_code = error_code
        self.param = param


def _error_fields(payload) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    values = []
    for key in ("error", "detail"):
        if key in payload:
            values.append((payload[key], True))
    response = payload.get("response")
    if isinstance(response, dict) and "error" in response:
        values.append((response["error"], True))
    values.append((payload, False))

    for value, is_error_object in values:
        if isinstance(value, dict):
            code = value.get("code") or (value.get("type") if is_error_object else "") or ""
            param = value.get("param") or payload.get("param") or ""
            if code:
                return str(code), str(param)
            value = value.get("message")
        if isinstance(value, str):
            match = _CODE_PATTERN.search(value)
            if match:
                return match.group(0), str(payload.get("param") or "")
    return "", ""


def _safe_label(value: str, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:100]
    return cleaned or default


def classify_api_error(status: int, payload, *, refresh: bool = False) -> ChatGPTPlanError:
    """Ordnet HTTP- und Admission-Fehler den in der Chat-Ketten-Spec definierten Klassen zu."""
    raw_code, raw_param = _error_fields(payload)
    code = _safe_label(raw_code, "")
    param = _safe_label(raw_param, "")
    description = f"ChatGPT-Plan HTTP {int(status)}" + (f" ({code})" if code else "")

    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTPlanError(description, cooldown_s=0, error_code=code, param=param)
    if code == "subscription_sharing_usage_limit_exceeded":
        return ChatGPTPlanError(description, cooldown_s=QUOTA_PAUSE_S, error_code=code)

    login_codes = {
        "subscription_sharing_invalid_user", "invalid_grant", "invalid_refresh_token",
        "token_expired", "refresh_token_expired", "refresh_token_invalidated",
        "refresh_token_reused",
    }
    if status == 401 or code in login_codes or (refresh and code in login_codes):
        return ChatGPTPlanError(
            description, disable_backend=True, requires_login=True, error_code=code,
        )

    if status == 403 or code == "invalid_client" or code in {
        "subscription_sharing_user_not_eligible", "subscription_sharing_route_not_supported",
    } or code.startswith("chatpass_v2_"):
        return ChatGPTPlanError(description, disable_backend=True, error_code=code)

    if status == 503 or code in {
        "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
    }:
        return ChatGPTPlanError(description, error_code=code)
    if status in {400, 422}:
        return ChatGPTPlanError(description, cooldown_s=0, error_code=code, param=param)
    return ChatGPTPlanError(description, error_code=code)


def _json_arguments(value, *, tool_name: str) -> dict:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"ChatGPT Tool-Argumente für {tool_name} sind leer")
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ChatGPT Tool-Argumente für {tool_name} enthalten ungültiges JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"ChatGPT Tool-Argumente für {tool_name} müssen ein JSON-Objekt sein")
    return value


def _arguments_json(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def tools_to_responses(tools: list[dict]) -> list[dict]:
    """Wandelt Mantis-Tools in einen einzelnen Responses-Namespace um."""
    # Annahme laut Doku 13.10.2026: Function-Tools werden im `tools`-Feld gruppiert.
    functions = []
    for tool in tools:
        function = tool.get("function") or tool
        functions.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        })
    if not functions:
        return []
    return [{
        "type": "namespace",
        "name": "mantis",
        "description": "Mantis-Assistent-Tools",
        "tools": functions,
    }]


def messages_to_responses_input(messages: list[dict]) -> list[dict]:
    """Konvertiert Mantis-Messages in Responses-Messages und Call/Output-Items."""
    def content_to_responses(content):
        if not isinstance(content, list):
            return content or ""
        result = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                result.append({"type": "input_text", "text": str(block.get("text", ""))})
            elif block.get("type") == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64" and source.get("data"):
                    media_type = source.get("media_type", "image/jpeg")
                    result.append({
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,{source['data']}",
                    })
        return result

    result = []
    pending_ids: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, str) and not content.strip():
                content = ""
            elif isinstance(content, list):
                content = content_to_responses(content)
                content = [
                    block for block in content
                    if block.get("type") != "input_text" or str(block.get("text", "")).strip()
                ]
            calls = message.get("tool_calls") or []
            if content and not calls and result and result[-1].get("role") == "assistant":
                previous = result[-1]["content"]
                result[-1]["content"] = _merge_responses_content(previous, content)
            elif content:
                result.append({"role": "assistant", "content": content})
            for call in calls:
                function = call.get("function") or {}
                call_id = call.get("id") or call.get("tool_use_id") or f"call_{uuid.uuid4().hex[:12]}"
                pending_ids.append(call_id)
                result.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name", ""),
                    "arguments": _arguments_json(function.get("arguments")),
                })
        elif role == "tool":
            call_id = message.get("tool_use_id") or message.get("tool_call_id")
            if not call_id:
                call_id = pending_ids.pop(0) if pending_ids else f"call_{uuid.uuid4().hex[:12]}"
            elif call_id in pending_ids:
                pending_ids.remove(call_id)
            result.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": str(message.get("content", "")),
            })
        else:
            content = content_to_responses(message.get("content", ""))
            if isinstance(content, str) and not content.strip():
                continue
            if isinstance(content, list):
                content = [
                    block for block in content
                    if block.get("type") != "input_text" or str(block.get("text", "")).strip()
                ]
                if not content:
                    continue
            entry = {"role": role, "content": content}
            if role == "user" and result and result[-1].get("role") == "user":
                result[-1]["content"] = _merge_responses_content(result[-1]["content"], content)
            else:
                result.append(entry)
    return result


def _merge_responses_content(previous, current):
    if isinstance(previous, str) and isinstance(current, str):
        return f"{previous}\n{current}"
    parts = []
    if isinstance(previous, str):
        if previous:
            parts.append({"type": "input_text", "text": previous})
    else:
        parts.extend(previous)
    if isinstance(current, str):
        if current:
            parts.append({"type": "input_text", "text": current})
    else:
        parts.extend(current)
    return parts


def _extract_responses_result(response: dict, tools: list[dict] | None) -> tuple[str, list[dict]]:
    output = response.get("output") or []
    if not isinstance(output, list):
        raise RuntimeError("Responses-API lieferte ungültige Output-Daten")
    text_parts = []
    required_by_name = {}
    response_name_to_tool_name = {}
    for tool in tools or []:
        function = tool.get("function") or tool
        params = function.get("parameters") or {}
        tool_name = function.get("name", "")
        required_by_name[tool_name] = params.get("required") or []
        for alias in (tool_name, f"mantis.{tool_name}", f"mantis/{tool_name}", f"mantis__{tool_name}"):
            previous = response_name_to_tool_name.get(alias)
            if previous is not None and previous != tool_name:
                raise RuntimeError("Mantis-Toolnamen sind für Responses-Namespaces nicht eindeutig")
            response_name_to_tool_name[alias] = tool_name

    calls = []
    for item in output:
        if not isinstance(item, dict):
            raise RuntimeError("Responses-API lieferte ein ungültiges Output-Item")
        if item.get("type") == "message":
            content = item.get("content") or []
            if not isinstance(content, list):
                raise RuntimeError("Responses-API lieferte ungültigen Nachrichtentext")
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        elif item.get("type") == "function_call":
            name = item.get("name")
            call_id = item.get("call_id")
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
                raise RuntimeError("Responses-API lieferte einen Tool-Call ohne Name oder call_id")
            tool_name = response_name_to_tool_name.get(name)
            if tool_name is None:
                raise RuntimeError("Responses-API nannte ein unbekanntes Mantis-Tool")
            if "arguments" not in item:
                required = required_by_name.get(tool_name, [])
                if required:
                    raise ValueError(f"ChatGPT Tool-Argumente fehlen für Pflichtargumente: {', '.join(required)}")
                arguments = {}
            else:
                arguments = _json_arguments(item["arguments"], tool_name=tool_name)
            calls.append({
                "id": call_id,
                "tool_use_id": call_id,
                "function": {"name": tool_name, "arguments": arguments},
            })
    response_text = response.get("output_text")
    text = "".join(text_parts)
    if not text and isinstance(response_text, str):
        text = response_text
    return text, calls


def _response_usage(response: dict) -> dict | None:
    usage = response.get("usage")
    return usage if isinstance(usage, dict) else None


class ChatGPTPlanBackend(AgentBackend):
    provider_name = "ChatGPT-Abo"

    def __init__(
        self,
        model: str,
        auth_file: str | Path,
        client: httpx.AsyncClient | None = None,
        purpose: str = "chat-agent",
    ):
        self._model = model
        self._auth_file = Path(auth_file).expanduser()
        self._client = client or httpx.AsyncClient(timeout=60)
        self._purpose = purpose
        self._permanent_error_logged = False

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_vision(self) -> bool:
        return True

    async def warmup(self) -> None:
        return None

    def _raise_api_error(self, status: int, payload, *, refresh: bool = False) -> None:
        error = classify_api_error(status, payload, refresh=refresh)
        if error.param:
            log.error(
                "ChatGPT-Plan unterstützt die angeforderte Fähigkeit nicht (param=%s)",
                redact_error_text(error.param),
            )
        if error.disable_backend and not self._permanent_error_logged:
            self._permanent_error_logged = True
            if error.requires_login:
                log.error("ChatGPT-Login erneuern: python3.14 -m core.auth.chatgpt_login")
            else:
                log.error(
                    "ChatGPT-Plan-Stufe dauerhaft deaktiviert (%s)",
                    redact_error_text(error.error_code or status),
                )
        raise error

    async def _read_error_payload(self, response: httpx.Response) -> dict:
        try:
            payload = json.loads(await response.aread())
        except (ValueError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    async def get_access_token(self) -> str:
        """Liefert ein Token und rotiert es spätestens fünf Minuten vor Ablauf."""
        async with _refresh_lock:
            auth = load_auth(self._auth_file)
            if not str(auth.get("client_id", "")).startswith("oaiapp_"):
                raise ValueError("ChatGPT-Login enthält keine ausgestellte Client-ID")
            expires_at = float(auth["expires_at"])
            if expires_at - time.time() > REFRESH_SKEW_S:
                return str(auth["access_token"])
            response = await self._client.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": auth["client_id"],
                "refresh_token": auth["refresh_token"],
                "resource": RESOURCE,
            })
            if response.is_error:
                self._raise_api_error(response.status_code, await self._read_error_payload(response), refresh=True)
            try:
                refreshed = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("ChatGPT-Refresh lieferte ungültiges JSON") from exc
            access_token = refreshed.get("access_token") if isinstance(refreshed, dict) else None
            refresh_token = refreshed.get("refresh_token") if isinstance(refreshed, dict) else None
            expires_in = refreshed.get("expires_in", 3600) if isinstance(refreshed, dict) else None
            if not all(isinstance(value, str) and value for value in (access_token, refresh_token)):
                raise RuntimeError("ChatGPT-Refresh lieferte keine rotierten Tokens")
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
                raise RuntimeError("ChatGPT-Refresh lieferte eine ungültige Gültigkeitsdauer")
            updated = {
                **auth,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            }
            save_auth(self._auth_file, updated)
            return access_token

    def _payload(self, messages, tools, force_tool_call) -> dict:
        payload = {
            "model": self._model,
            "input": messages_to_responses_input(messages),
            "stream": True,
            "store": False,
        }
        if tools:
            payload["tools"] = tools_to_responses(tools)
            if force_tool_call:
                payload["tool_choice"] = "required"
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
        # Responses-API-Preview nimmt hier weder temperature noch max_output_tokens an.
        access_token = await self.get_access_token()
        payload = self._payload(messages, tools, force_tool_call)
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        text_parts: list[str] = []
        usage = None
        usage_recorded = False

        def record_usage_once() -> None:
            nonlocal usage_recorded
            if not usage_recorded and usage is not None:
                usage_recorded = True
                self._record_usage(usage)

        streamed_items: list[dict] = []
        try:
            async with self._client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.is_error:
                    self._raise_api_error(response.status_code, await self._read_error_payload(response))
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError as exc:
                        raise RuntimeError("Responses-Stream enthält ungültiges JSON") from exc
                    if not isinstance(event, dict):
                        raise RuntimeError("Responses-Stream enthält ein ungültiges Ereignis")
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise RuntimeError("Responses-Stream enthält ungültigen Text")
                        text_parts.append(delta)
                        if stream_cb:
                            try:
                                await stream_cb(delta)
                            except Exception:
                                pass
                    elif event_type == "response.output_item.done":
                        # Mit store=false bleibt response.completed.output leer;
                        # die fertigen Items kommen nur hier (live geprüft 13.10.2026).
                        item = event.get("item")
                        if isinstance(item, dict):
                            streamed_items.append(item)
                    elif event_type == "response.failed":
                        error_code, _ = _error_fields(event)
                        if error_code:
                            self._raise_api_error(500, event)
                        raise RuntimeError("Responses-API meldet response.failed")
                    elif event_type == "response.incomplete":
                        raise RuntimeError("Responses-API meldet response.incomplete")
                    elif event_type == "error":
                        self._raise_api_error(500, event)
                    elif event_type == "response.completed":
                        completed = event.get("response")
                        if not isinstance(completed, dict) or completed.get("status", "completed") != "completed":
                            raise RuntimeError("Responses-API meldet keinen erfolgreichen Abschluss")
                        usage = _response_usage(completed)
                        if not completed.get("output") and streamed_items:
                            completed = {**completed, "output": streamed_items}
                        text, calls = _extract_responses_result(completed, tools)
                        if not text and text_parts:
                            text = "".join(text_parts)
                        record_usage_once()
                        if stream_cb and text and not text_parts:
                            try:
                                await stream_cb(text)
                            except Exception:
                                pass
                        return text, calls
            raise RuntimeError("Responses-Stream endete ohne response.completed")
        except BaseException:
            record_usage_once()
            raise

    def _record_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        try:
            from core import llm_usage
            llm_usage.record(
                "chatgpt-plan",
                self._model,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                purpose=f"{self._purpose} (Abo)",
                actual_cost=0.0,
            )
        except Exception:
            pass


