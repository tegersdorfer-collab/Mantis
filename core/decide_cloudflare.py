"""Cloudflare Workers AI Backend für jevkit."""
from __future__ import annotations

import json
from typing import Any

import httpx
from jevkit.backends import BackendError, RawResponse, RETRY_STATUS
from core.error_redaction import redact_error_text

MAX_CLEF_QUESTION_TEXT = 4096


class CloudflareQuotaError(BackendError):
    """Cloudflare hat das Tageskontingent für Workers AI ausgeschöpft."""


def _has_error_code(value: Any, expected: int) -> bool:
    if isinstance(value, dict):
        code = value.get("code")
        if code == expected or str(code) == str(expected):
            return True
        return any(_has_error_code(child, expected) for child in value.values())
    if isinstance(value, list):
        return any(_has_error_code(child, expected) for child in value)
    return False


def _is_daily_quota(status_code: int, payload: Any) -> bool:
    # Ein nacktes 429 kann auch ein kurzes Rate-Limit oder "Out of Capacity" (3040)
    # sein; das soll nur den normalen Breaker auslösen, nicht Clef bis Mitternacht sperren.
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if _has_error_code(errors, 4006):
        return True
    text = json.dumps(errors, ensure_ascii=False).casefold()
    return any(hint in text for hint in ("neuron", "daily", "allocation", "quota"))


def _question_text(value: Any) -> str:
    """Clef erhält Instructions als begrenzten Text, auch wenn Jev JSON nutzt."""
    if isinstance(value, str):
        return value.strip()[:MAX_CLEF_QUESTION_TEXT]
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:MAX_CLEF_QUESTION_TEXT]
    except (TypeError, ValueError):
        return ""


def _bound_question_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_CLEF_QUESTION_TEXT]
    if isinstance(value, dict):
        return {key: _bound_question_strings(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_bound_question_strings(child) for child in value]
    return value


def _normalize_questions(questions: Any) -> dict[str, dict]:
    """Lässt leere/frühe Fehlformen weg und begrenzt Textfelder vor dem HTTP-Call."""
    if not isinstance(questions, dict):
        raise BackendError("Cloudflare questions must be a mapping", retryable=False)

    normalized: dict[str, dict] = {}
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or not question_id.strip() or not isinstance(question, dict):
            continue
        instructions = _question_text(question.get("instructions"))
        if not instructions:
            continue
        normalized_question = _bound_question_strings(question)
        normalized_question["instructions"] = instructions
        normalized[question_id] = normalized_question

    if not normalized:
        raise BackendError("Cloudflare questions contain no non-empty instructions", retryable=False)
    return normalized


def _find_field_errors(value: Any) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == "fielderrors":
                return child
        for child in value.values():
            found = _find_field_errors(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_field_errors(child)
            if found is not None:
                return found
    elif isinstance(value, str):
        marker = "fieldErrors"
        if marker in value:
            return value[value.index(marker):]
    return None


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, secrets) for child in value]
    return value


def _http_error_detail(status_code: int, response_text: str, payload: Any,
                       secrets: tuple[str, ...]) -> str:
    if status_code == 422:
        field_errors = _find_field_errors(payload)
        if field_errors is not None:
            detail = json.dumps(_redact(field_errors, secrets), ensure_ascii=False, sort_keys=True)
            return f"fieldErrors: {detail}"
    detail = redact_error_text(response_text)
    return _redact(detail, secrets)[:200]


class CloudflareBackend:
    """Adaptiert Cloudflares Workers-AI-Envelope auf jevkits RawResponse."""

    def __init__(
        self,
        account_id: str,
        auth_token: str,
        *,
        model: str = "clef-flash",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_id = str(account_id or "").strip()
        self.auth_token = str(auth_token or "").strip()
        if not self.account_id or not self.auth_token:
            raise BackendError("Cloudflare account ID and auth token are required", retryable=False)
        self.default_model = model
        self.transport = transport
        self.url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/@cf/cloudflare/{self.default_model}"
        )

    async def ask(
        self,
        state: Any,
        questions: dict[str, dict],
        *,
        model: str | None = None,
        timeout_s: float = 10.0,
    ) -> RawResponse:
        actual_model = model or self.default_model
        body = {"model": actual_model, "state": state, "questions": _normalize_questions(questions)}
        payload = json.dumps(body, ensure_ascii=False).encode()
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }
        url = self.url if actual_model == self.default_model else (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/@cf/cloudflare/{actual_model}"
        )

        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self.transport) as client:
                response = await client.post(url, content=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise BackendError(f"network error: {exc!r}", retryable=True) from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.status_code != 200:
            if _is_daily_quota(response.status_code, data):
                raise CloudflareQuotaError("Cloudflare daily Neuron quota exhausted", retryable=False)
            detail = _http_error_detail(
                response.status_code,
                response.text,
                data,
                (self.auth_token, self.account_id),
            )
            raise BackendError(
                f"HTTP {response.status_code}: {detail}",
                retryable=response.status_code in RETRY_STATUS,
            )

        if not isinstance(data, dict):
            raise BackendError("broken Cloudflare response: expected an object", retryable=False)
        if data.get("success") is False:
            if _is_daily_quota(response.status_code, data):
                raise CloudflareQuotaError("Cloudflare daily Neuron quota exhausted", retryable=False)
            raise BackendError("Cloudflare response has success=false", retryable=False)

        result = data.get("result")
        if not isinstance(result, dict):
            raise BackendError("broken Cloudflare response: missing result", retryable=False)
        answers = result.get("answers")
        if not isinstance(answers, dict):
            raise BackendError("broken Cloudflare response: answers is not an object", retryable=False)
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            raise BackendError("broken Cloudflare response: usage is not an object", retryable=False)
        return RawResponse(str(result.get("model", "")), answers, usage)
