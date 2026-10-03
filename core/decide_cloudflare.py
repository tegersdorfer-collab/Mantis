"""Cloudflare Workers AI Backend für jevkit."""
from __future__ import annotations

import json
from typing import Any

import httpx
from jevkit.backends import BackendError, RawResponse, RETRY_STATUS


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
        body = {"model": actual_model, "state": state, "questions": questions}
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
            raise BackendError(
                f"HTTP {response.status_code}: {response.text[:200]}",
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
