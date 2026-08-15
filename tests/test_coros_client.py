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


def _sse_multi(frames: list) -> httpx.Response:
    """SSE-Body aus mehreren data:-Zeilen bauen. Strings gehen roh raus (z. B. ein
    Keep-Alive ohne JSON-Inhalt), Dicts werden als JSON-RPC-Nachricht serialisiert."""
    lines = []
    for frame in frames:
        text = frame if isinstance(frame, str) else json.dumps(frame)
        lines.append(f"data: {text}\n\n")
    return httpx.Response(200, text="".join(lines),
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


def test_handshake_retries_once_on_401():
    """Der 401-Retry gilt auch für den initialize-Aufruf, nicht nur für tools/call —
    das war vorher unbelegt und lief über eine separate, nicht geteilte Codestelle."""
    calls = {"initialize": 0}
    refreshes = []

    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            calls["initialize"] += 1
            if calls["initialize"] == 1:
                return httpx.Response(401)
            return _json_response(req, {"protocolVersion": "2025-06-18"},
                                  headers={"Mcp-Session-Id": "sess-99"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return _json_response(req, {"content": []})

    def token_provider(force_refresh=False):
        refreshes.append(force_refresh)
        return "tok"

    c = _client(handler, token_provider)
    c.call_tool("queryUserInfo", {})
    assert calls["initialize"] == 2
    assert True in refreshes
    assert c._session_id == "sess-99"


def test_notifications_initialized_failure_raises_instead_of_going_ready():
    """Lehnt COROS die notifications/initialized-Notification ab, darf der Client
    nicht stillschweigend `ready` werden — sonst schlägt jeder spätere tools/call
    ohne erkennbaren Zusammenhang zum Handshake fehl."""
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(400)
        return _json_response(req, {"content": []})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).call_tool("queryUserInfo", {})


def test_sse_skips_non_json_keepalive_line_before_real_frame():
    """Ein data:-Frame, der kein JSON ist (Keep-Alive), darf nicht crashen — er wird
    übersprungen, bis der echte Ergebnis-Frame kommt."""
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        real = {"jsonrpc": "2.0", "id": body.get("id"),
                "result": {"structuredContent": {"ok": True}}}
        return _sse_multi(["keep-alive", real])

    result = _client(handler).call_tool("queryUserInfo", {})
    assert payload(result) == {"ok": True}


def test_sse_multi_frame_body_returns_the_frame_with_result():
    """Mehrere data:-Frames (z. B. eine Progress-Notification vor dem eigentlichen
    Ergebnis) — der Client muss den richtigen Frame finden, nicht den ersten."""
    def handler(req):
        body = _rpc(req)
        if body.get("method") == "initialize":
            return _json_response(req, {"protocolVersion": "2025-06-18"})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        real = {"jsonrpc": "2.0", "id": body.get("id"),
                "result": {"structuredContent": {"ok": True}}}
        return _sse_multi([progress, real])

    result = _client(handler).call_tool("queryUserInfo", {})
    assert payload(result) == {"ok": True}


def test_close_leaves_injected_http_client_open():
    """close() darf nur einen selbst erzeugten httpx.Client schließen, nicht einen,
    den der Aufrufer übergeben hat — der könnte anderswo weiterverwendet werden."""
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    c = CorosClient(base_url=URL, http=http, token_provider=lambda force_refresh=False: "tok")
    c.close()
    assert http.is_closed is False
    http.close()


def test_close_closes_self_created_http_client():
    c = CorosClient(base_url=URL, token_provider=lambda force_refresh=False: "tok")
    c.close()
    assert c._http.is_closed is True


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
