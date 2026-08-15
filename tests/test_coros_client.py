"""Tests für den COROS-MCP-Client (domains/coros/client.py).

Netzwerk wird komplett über httpx.MockTransport gefälscht — kein echter Aufruf.
"""

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros.client import CorosClient, CorosToolError, payload

URL = "https://mcpeu.example/mcp"


def _rpc(req: httpx.Request) -> dict:
    return json.loads(req.content)


def _json_response(req: httpx.Request, result: dict, headers: dict | None = None):
    body = {"jsonrpc": "2.0", "id": _rpc(req).get("id"), "result": result}
    return httpx.Response(200, json=body, headers=headers or {})


def _sse_response(req: httpx.Request, result: dict):
    body = {"jsonrpc": "2.0", "id": _rpc(req).get("id"), "result": result}
    text = f"event: message\ndata: {json.dumps(body)}\n\n"
    return httpx.Response(200, text=text,
                          headers={"Content-Type": "text/event-stream"})


def _client(handler, token_provider=None) -> CorosClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CorosClient(base_url=URL, http=http,
                       token_provider=token_provider or (lambda force_refresh=False: "tok"))


def test_handshake_then_tool_call_returns_result():
    seen = []

    def handler(req):
        body = _rpc(req)
        seen.append(body.get("method"))
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"},
                                  headers={"Mcp-Session-Id": "sess-1"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _json_response(req, {"content": [{"type": "text", "text": "{\"steps\": 8000}"}]})

    result = _client(handler).call_tool("queryDailyHealthData", {"day": "2026-08-15"})
    assert seen == ["initialize", "notifications/initialized", "tools/call"]
    assert payload(result) == {"steps": 8000}


def test_session_id_is_echoed_on_later_requests():
    headers_seen = []

    def handler(req):
        body = _rpc(req)
        headers_seen.append(req.headers.get("mcp-session-id"))
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"},
                                  headers={"Mcp-Session-Id": "sess-42"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _json_response(req, {"content": []})

    _client(handler).call_tool("queryUserInfo", {})
    assert headers_seen[0] is None          # initialize kennt noch keine Session
    assert headers_seen[1:] == ["sess-42", "sess-42"]


def test_sse_framed_response_is_parsed():
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _sse_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _sse_response(req, {"structuredContent": {"hrv": 61}})

    result = _client(handler).call_tool("querySleepHrv", {})
    assert payload(result) == {"hrv": 61}


def test_tool_error_raises():
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _json_response(req, {"isError": True,
                                    "content": [{"type": "text", "text": "kaputt"}]})

    with pytest.raises(CorosToolError, match="kaputt"):
        _client(handler).call_tool("queryDailyHealthData", {})


def test_jsonrpc_error_raises():
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "error": {"code": -32602, "message": "bad args"}})

    with pytest.raises(CorosToolError, match="bad args"):
        _client(handler).call_tool("queryDailyHealthData", {})


def test_401_triggers_exactly_one_refresh_and_retry():
    calls = {"tools": 0}
    refreshes = []

    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        calls["tools"] += 1
        if calls["tools"] == 1:
            return httpx.Response(401)
        return _json_response(req, {"content": [{"type": "text", "text": "{}"}]})

    def token_provider(force_refresh=False):
        refreshes.append(force_refresh)
        return "tok"

    _client(handler, token_provider).call_tool("queryUserInfo", {})
    assert calls["tools"] == 2
    assert True in refreshes


def test_persistent_401_gives_up_instead_of_looping():
    calls = {"tools": 0}

    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        calls["tools"] += 1
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).call_tool("queryUserInfo", {})
    assert calls["tools"] == 2


def test_list_tools_returns_catalogue():
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _json_response(req, {"tools": [{"name": "queryUserInfo", "inputSchema": {}}]})

    tools = _client(handler).list_tools()
    assert [t["name"] for t in tools] == ["queryUserInfo"]


def test_payload_falls_back_to_plain_text():
    assert payload({"content": [{"type": "text", "text": "kein JSON"}]}) == "kein JSON"


def test_payload_empty_result_is_empty_dict():
    assert payload({"content": []}) == {}
