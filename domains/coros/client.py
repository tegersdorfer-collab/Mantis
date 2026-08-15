"""MCP-Client für COROS über Streamable HTTP.

Handgeschrieben auf dem vorhandenen httpx statt über das offizielle mcp-SDK: das
SDK (2.0.0) zieht httpx2, starlette, uvicorn, sse-starlette und opentelemetry nach
— ein kompletter zweiter HTTP-Stack für einen reinen Client. Gebraucht werden nur
drei Nachrichten: initialize, notifications/initialized, tools/call.

Der Server antwortet je nach Laune als application/json oder als text/event-stream;
beides wird hier gleich behandelt.
"""
import json
import logging

import httpx

import config
from domains.coros import oauth

log = logging.getLogger("mantis.coros")

PROTOCOL_VERSION = "2025-06-18"


class CorosToolError(RuntimeError):
    """Das MCP hat den Aufruf abgelehnt oder einen Tool-Fehler gemeldet."""


def payload(result: dict):
    """Die Nutzlast aus einem MCP-Tool-Result ziehen.

    Bevorzugt `structuredContent`; sonst der erste Text-Block, als JSON geparst,
    und falls das kein JSON ist, der rohe Text. Leeres Result → {}.
    """
    if not result:
        return {}
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for block in result.get("content") or []:
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return text
    return {}


class CorosClient:
    """Eine MCP-Sitzung. Nicht thread-safe; pro Import-Lauf einen bauen."""

    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None,
                 token_provider=oauth.access_token):
        self.base_url = base_url or config.COROS_MCP_URL
        self._http = http or httpx.Client(timeout=60)
        self._token_provider = token_provider
        self._session_id: str | None = None
        self._ready = False
        self._next_id = 0

    # ── intern ──────────────────────────────────────────────────────────────
    def _headers(self, force_refresh: bool = False) -> dict:
        h = {
            "Authorization": f"Bearer {self._token_provider(force_refresh=force_refresh)}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, body: dict, force_refresh: bool = False) -> httpx.Response:
        return self._http.post(self.base_url, json=body, headers=self._headers(force_refresh))

    @staticmethod
    def _decode(resp: httpx.Response) -> dict:
        """JSON-RPC-Antwort aus JSON- oder SSE-Body holen."""
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if "result" in msg or "error" in msg:
                        return msg
            raise CorosToolError("SSE-Antwort ohne verwertbares data-Feld")
        return resp.json()

    def _request(self, method: str, params: dict) -> dict:
        """Eine JSON-RPC-Anfrage; bei 401 genau ein Refresh-Versuch."""
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}

        resp = self._post(body)
        if resp.status_code == 401:
            log.info("COROS: 401 — Token wird erneuert und der Aufruf einmal wiederholt")
            resp = self._post(body, force_refresh=True)
        resp.raise_for_status()

        msg = self._decode(resp)
        if "error" in msg:
            raise CorosToolError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def _handshake(self) -> None:
        if self._ready:
            return
        self._next_id += 1
        body = {
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mantis", "version": "1.0"},
            },
        }
        resp = self._post(body)
        if resp.status_code == 401:
            resp = self._post(body, force_refresh=True)
        resp.raise_for_status()
        self._decode(resp)
        self._session_id = resp.headers.get("Mcp-Session-Id") or self._session_id
        # Pflicht laut Spec: der Server darf vorher keine Tool-Aufrufe annehmen.
        self._http.post(self.base_url, headers=self._headers(),
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    # ── öffentlich ──────────────────────────────────────────────────────────
    def list_tools(self) -> list[dict]:
        self._handshake()
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        self._handshake()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise CorosToolError(f"{name}: {payload(result)}")
        return result

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
