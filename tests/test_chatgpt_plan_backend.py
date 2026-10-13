import asyncio
import json
import secrets
import time

import httpx
import pytest

from core.auth.chatgpt import DIRECT_TOKEN_SCOPE, save_auth
from core.backends.chatgpt_plan import (
    ChatGPTPlanError,
    ChatGPTPlanBackend,
    classify_api_error,
    _extract_responses_result,
    messages_to_responses_input,
    tools_to_responses,
)


def _auth_file(tmp_path, *, expires_at=None, access_token=None, refresh_token=None):
    path = tmp_path / "chatgpt_auth.json"
    save_auth(path, {
        "client_id": "oaiapp_test",
        "ext_agent_host_id": "host-test",
        "access_token": access_token or secrets.token_urlsafe(32),
        "refresh_token": refresh_token or secrets.token_urlsafe(32),
        "expires_at": expires_at if expires_at is not None else time.time() + 3600,
        "scope": f"openid {DIRECT_TOKEN_SCOPE}",
    })
    return path


def _sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def test_responses_converters_use_standard_function_call_and_output_items():
    tools = tools_to_responses([{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Find a record",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }])
    input_items = messages_to_responses_input([
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "Search"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-existing",
            "function": {"name": "lookup", "arguments": {"query": "x"}},
        }]},
        {"role": "tool", "tool_use_id": "call-existing", "content": "found"},
    ])

    assert tools == [{
        "type": "namespace",
        "name": "mantis",
        "description": "Mantis-Assistent-Tools",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "description": "Find a record",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }],
    }]
    assert input_items == [
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "Search"},
        {"type": "function_call", "call_id": "call-existing", "name": "lookup", "arguments": '{"query": "x"}'},
        {"type": "function_call_output", "call_id": "call-existing", "output": "found"},
    ]


def test_responses_request_always_streams_and_disables_storage_and_tool_roundtrips(tmp_path, monkeypatch):
    seen = {}
    access_value = secrets.token_urlsafe(32)
    events = [
        {"type": "response.output_text.delta", "delta": "done"},
        {"type": "response.completed", "response": {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
                {"type": "function_call", "call_id": "call-next", "name": "mantis/lookup", "arguments": '{"query":"next"}'},
            ],
            "usage": {"input_tokens": 31, "output_tokens": 4},
        }},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(*events), headers={"content-type": "text/event-stream"})

    records = []
    from core import llm_usage
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    path = _auth_file(tmp_path, access_token=access_value)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = ChatGPTPlanBackend(model="gpt-6-luna", auth_file=path, client=client)

    text, calls = asyncio.run(backend.call(
        [{"role": "user", "content": "search"}],
        tools=[{"type": "function", "function": {"name": "lookup", "description": "Find", "parameters": {"type": "object", "properties": {}}}}],
        stream_cb=None,
        temperature=0.7,
        max_tokens=500,
    ))

    payload = seen["payload"]
    assert seen["request"].url == "https://api.openai.com/v1/responses"
    assert seen["request"].headers["authorization"] == f"Bearer {access_value}"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["model"] == "gpt-6-luna"
    assert payload["tools"] == [{
        "type": "namespace",
        "name": "mantis",
        "description": "Mantis-Assistent-Tools",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "description": "Find",
            "parameters": {"type": "object", "properties": {}},
        }],
    }]
    assert not {"temperature", "top_p", "max_output_tokens", "metadata", "user", "truncation", "previous_response_id"} & payload.keys()
    assert text == "done"
    assert calls == [{
        "id": "call-next", "tool_use_id": "call-next",
        "function": {"name": "lookup", "arguments": {"query": "next"}},
    }]
    assert records == [(('chatgpt-plan', 'gpt-6-luna', 31, 4), {
        "purpose": "chat-agent (Abo)", "actual_cost": 0.0,
    })]


@pytest.mark.parametrize("remote_name", ["lookup", "mantis.lookup", "mantis/lookup", "mantis__lookup"])
def test_responses_maps_documented_namespace_name_spellings(remote_name):
    tool = {"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "properties": {}},
    }}

    _, calls = _extract_responses_result({"output": [{
        "type": "function_call", "call_id": "call-1", "name": remote_name, "arguments": "{}",
    }]}, [tool])

    assert calls == [{
        "id": "call-1", "tool_use_id": "call-1",
        "function": {"name": "lookup", "arguments": {}},
    }]


def test_responses_rejects_unknown_function_names_instead_of_guessing():
    tool = {"type": "function", "function": {"name": "lookup"}}

    with pytest.raises(RuntimeError, match="unbekanntes Mantis-Tool"):
        _extract_responses_result({"output": [{
            "type": "function_call", "call_id": "call-1", "name": "mantis/other", "arguments": "{}",
        }]}, [tool])


@pytest.mark.parametrize("event", [
    {"type": "response.failed", "response": {"error": {"code": "server_error"}}},
    {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
])
def test_responses_rejects_failed_and_incomplete_events(tmp_path, event):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(event), headers={"content-type": "text/event-stream"})

    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=_auth_file(tmp_path),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(backend.call([{"role": "user", "content": "hi"}], None, None, 0.2, 50))


def test_responses_requires_completed_event_and_valid_tool_arguments(tmp_path):
    bodies = [
        _sse({"type": "response.output_text.delta", "delta": "partial"}),
        _sse({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "function_call", "call_id": "call-bad", "name": "lookup", "arguments": "{bad"},
        ]}}),
    ]
    for body in bodies:
        def handler(request: httpx.Request, body=body) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        backend = ChatGPTPlanBackend(
            model="gpt-6-luna", auth_file=_auth_file(tmp_path),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(backend.call([{"role": "user", "content": "hi"}], None, None, 0.2, 50))


@pytest.mark.parametrize(("event", "cooldown", "disabled", "requires_login", "param"), [
    ({"type": "error", "code": "subscription_sharing_usage_unavailable"}, None, False, False, ""),
    ({"type": "error", "code": "subscription_sharing_usage_limit_exceeded"}, 1800, False, False, ""),
    ({"type": "error", "message": "subscription_sharing_usage_limit_exceeded"}, 1800, False, False, ""),
    ({"type": "error", "code": "subscription_sharing_invalid_user"}, None, True, True, ""),
    ({"type": "error", "code": "invalid_client"}, None, True, False, ""),
    ({"type": "error", "code": "subscription_sharing_unsupported_capability", "param": "tools"}, None, False, False, "tools"),
    ({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_invalid_user"}}}, None, True, True, ""),
])
def test_sse_errors_use_the_same_failover_classes_as_http_errors(
    tmp_path, event, cooldown, disabled, requires_login, param,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(event), headers={"content-type": "text/event-stream"})

    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=_auth_file(tmp_path),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ChatGPTPlanError) as caught:
        asyncio.run(backend.call([{"role": "user", "content": "hi"}], None, None, 0.2, 50))

    assert caught.value.cooldown_s == cooldown
    assert caught.value.disable_backend is disabled
    assert caught.value.requires_login is requires_login
    assert caught.value.param == param


def test_stream_callback_cancellation_does_not_record_completed_usage_twice(tmp_path, monkeypatch):
    event = {"type": "response.completed", "response": {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(event), headers={"content-type": "text/event-stream"})

    records = []
    from core import llm_usage
    monkeypatch.setattr(llm_usage, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=_auth_file(tmp_path),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def cancel_final_callback(_text):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend.call(
            [{"role": "user", "content": "hi"}], None, cancel_final_callback, 0.2, 50,
        ))

    assert records == [(('chatgpt-plan', 'gpt-6-luna', 10, 3), {
        "purpose": "chat-agent (Abo)", "actual_cost": 0.0,
    })]


@pytest.mark.parametrize(("status", "payload", "cooldown", "disabled"), [
    (503, {"error": {"code": "subscription_sharing_usage_unavailable"}}, None, False),
    (400, {"detail": "subscription_sharing_user_unavailable"}, None, False),
    (429, {"detail": {"code": "subscription_sharing_usage_limit_exceeded"}}, 1800, False),
    (401, {"error": {"code": "subscription_sharing_invalid_user"}}, None, True),
    (400, {"error": {"code": "invalid_grant"}}, None, True),
    (400, {"error": {"code": "invalid_refresh_token"}}, None, True),
    (400, {"error": {"code": "token_expired"}}, None, True),
    (400, {"error": {"code": "refresh_token_expired"}}, None, True),
    (400, {"error": {"code": "refresh_token_invalidated"}}, None, True),
    (400, {"error": {"code": "refresh_token_reused"}}, None, True),
    (403, {"error": {"code": "subscription_sharing_user_not_eligible"}}, None, True),
    (403, {"error": {"code": "subscription_sharing_route_not_supported"}}, None, True),
    (403, {"detail": "chatpass_v2_access_denied"}, None, True),
    (400, {"error": {"code": "invalid_client"}}, None, True),
    (400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools"}}, None, False),
    (400, {"error": {"code": "invalid_request"}}, None, False),
])
def test_api_error_classification_controls_pause_and_permanent_disable(status, payload, cooldown, disabled):
    error = classify_api_error(status, payload)

    assert error.cooldown_s == cooldown
    assert error.disable_backend is disabled


def test_unsupported_capability_logs_error_param(caplog):
    backend = ChatGPTPlanBackend(model="gpt-6-luna", auth_file="/tmp/unused-chatgpt-auth.json")
    with pytest.raises(ChatGPTPlanError):
        backend._raise_api_error(400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools"}})
    assert "param=tools" in caplog.text


def test_permanent_login_error_is_logged_once(caplog):
    backend = ChatGPTPlanBackend(model="gpt-6-luna", auth_file="/tmp/unused-chatgpt-auth.json")
    payload = {"error": {"code": "invalid_refresh_token"}}

    with caplog.at_level("ERROR"):
        for _ in range(2):
            with pytest.raises(ChatGPTPlanError):
                backend._raise_api_error(400, payload, refresh=True)

    assert caplog.text.count("ChatGPT-Login erneuern:") == 1


@pytest.mark.parametrize(("status", "payload", "cooldown", "disabled"), [
    (503, {"detail": "subscription_sharing_usage_unavailable"}, None, False),
    (429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, 1800, False),
    (401, {"detail": {"code": "subscription_sharing_invalid_user"}}, None, True),
    (403, {"error": {"code": "subscription_sharing_route_not_supported"}}, None, True),
    (400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools"}}, None, False),
])
def test_http_errors_are_classified_before_they_reach_fallback(tmp_path, status, payload, cooldown, disabled):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=_auth_file(tmp_path),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ChatGPTPlanError) as caught:
        asyncio.run(backend.call([{"role": "user", "content": "hi"}], None, None, 0.2, 50))

    assert caught.value.cooldown_s == cooldown
    assert caught.value.disable_backend is disabled


def test_refresh_uses_rotated_token_without_scope_and_saves_both_tokens_atomically(tmp_path):
    seen = []
    refresh_value = secrets.token_urlsafe(32)
    rotated_access = secrets.token_urlsafe(32)
    rotated_refresh = secrets.token_urlsafe(32)
    path = _auth_file(tmp_path, expires_at=time.time() + 30, refresh_token=refresh_value)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(httpx.QueryParams(request.content.decode()))))
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": rotated_access, "refresh_token": rotated_refresh, "expires_in": 3600})
        return httpx.Response(200, content=_sse({"type": "response.completed", "response": {
            "status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 1},
        }}), headers={"content-type": "text/event-stream"})

    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=path,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    access = asyncio.run(backend.get_access_token())
    stored = json.loads(path.read_text())

    assert access == rotated_access
    assert seen[0][0] == "https://auth.openai.com/api/accounts/oauth/token"
    assert seen[0][1] == {
        "grant_type": "refresh_token", "client_id": "oaiapp_test",
        "refresh_token": refresh_value, "resource": "https://api.openai.com/v1",
    }
    assert "scope" not in seen[0][1]
    assert stored["access_token"] == rotated_access
    assert stored["refresh_token"] == rotated_refresh
    assert stored["expires_at"] > time.time() + 3500


def test_refresh_is_serialized_across_concurrent_backend_calls(tmp_path):
    count = 0
    refreshed_access = secrets.token_urlsafe(32)
    refreshed_token = secrets.token_urlsafe(32)
    path = _auth_file(tmp_path, expires_at=time.time() + 30)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.url.path.endswith("/oauth/token"):
            count += 1
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"access_token": refreshed_access, "refresh_token": refreshed_token, "expires_in": 3600})
        raise AssertionError("Responses-Aufruf in Token-Test unerwartet")

    backend = ChatGPTPlanBackend(
        model="gpt-6-luna", auth_file=path,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def run():
        return await asyncio.gather(backend.get_access_token(), backend.get_access_token())

    assert asyncio.run(run()) == [refreshed_access, refreshed_access]
    assert count == 1


def test_chatgpt_login_tokens_are_redacted_from_error_text(tmp_path):
    from core.error_redaction import redact_error_text

    access_value = secrets.token_urlsafe(32)
    refresh_value = secrets.token_urlsafe(32)
    save_auth(_auth_file(tmp_path), {
        "client_id": "oaiapp-test", "ext_agent_host_id": "host-test",
        "access_token": access_value, "refresh_token": refresh_value,
        "expires_at": time.time() + 3600, "scope": DIRECT_TOKEN_SCOPE,
    })
    result = redact_error_text(f"{access_value} {refresh_value}")

    assert access_value not in result
    assert refresh_value not in result
    assert result.count("[REDACTED]") == 2
