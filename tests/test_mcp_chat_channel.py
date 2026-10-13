from types import SimpleNamespace

import httpx

from web import mcp_server


def test_mcp_chat_uses_chat_payload_and_records_mcp_channel(monkeypatch):
    calls = []

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return SimpleNamespace(json=lambda: {"response": "ok"}, text="")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("web.client_auth.dashboard_url", lambda: "http://mantis.local")
    monkeypatch.setattr("web.client_auth.dashboard_headers", lambda: {"Authorization": "Bearer test"})

    response = mcp_server._handle_mcp_call("mantis_chat", {"message": "hello"})

    assert response == "ok"
    assert calls == [
        (
            "http://mantis.local/api/chat",
            {"Authorization": "Bearer test"},
            {"text": "hello", "channel": "mcp"},
            30,
        )
    ]
