"""
web/mcp_server.py

Mantis als MCP-Server für Claude Code.
Exponiert Mantis' Kernfunktionen als MCP-Tools über SSE oder stdio.

Einbinden in Claude Code: ~/.claude/claude_desktop_config.json
{
  "mcpServers": {
    "mantis": {
      "command": "python3",
      "args": ["/Users/timoegersdorfer/Mantis/web/mcp_server.py"],
      "env": {}
    }
  }
}

Oder via HTTP (wenn Mantis läuft): GET /mcp/tools, POST /mcp/call
"""
import json
import sys
import logging

log = logging.getLogger(__name__)

MCP_TOOLS = [
    {
        "name": "mantis_chat",
        "description": "Schick Mantis eine Nachricht und erhalte seine Antwort. Nutze ihn für Kontext über Timos Leben, Gesundheit, Tasks, Kalender, Erinnerungen.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Deine Frage oder Anweisung an Mantis"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "mantis_memory_search",
        "description": "Durchsucht Mantis' Langzeit-Gedächtnis semantisch nach relevanten Fakten über Timo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriff oder Frage"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mantis_brain_search",
        "description": "Durchsucht Mantis' Second Brain (Notizen, Projekte, Ressourcen).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriff"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mantis_get_tasks",
        "description": "Gibt Timos offene Tasks zurück.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter: open, in_progress, done"}
            }
        }
    },
    {
        "name": "mantis_get_health",
        "description": "Gibt aktuelle Gesundheitsdaten zurück (HRV, Schlaf, Schritte etc.).",
        "inputSchema": {"type": "object", "properties": {}}
    },
]


def _handle_mcp_call(tool: str, args: dict) -> str:
    """Führt einen MCP-Tool-Call aus. Synchron, für stdio-Modus."""
    import httpx
    from web.client_auth import dashboard_url, dashboard_headers
    base = dashboard_url()
    headers = dashboard_headers()

    try:
        if tool == "mantis_chat":
            r = httpx.post(f"{base}/api/chat", headers=headers, json={"message": args["message"]}, timeout=30)
            return r.json().get("response", r.text)

        elif tool == "mantis_memory_search":
            r = httpx.get(f"{base}/api/memory/search", headers=headers,
                          params={"q": args["query"]}, timeout=10)
            items = r.json() if r.status_code == 200 else []
            return "\n".join(f"- {m.get('content','')}" for m in items[:5]) or "Nichts gefunden."

        elif tool == "mantis_brain_search":
            r = httpx.get(f"{base}/api/brain/search", headers=headers,
                          params={"q": args["query"], "limit": 5}, timeout=10)
            items = r.json() if r.status_code == 200 else []
            return "\n".join(f"- [{n.get('title')}] {n.get('content','')[:120]}" for n in items) or "Nichts gefunden."

        elif tool == "mantis_get_tasks":
            status = args.get("status", "open")
            r = httpx.get(f"{base}/api/tasks", headers=headers, params={"status": status}, timeout=10)
            tasks = r.json() if r.status_code == 200 else []
            if isinstance(tasks, list):
                return "\n".join(f"- [{t.get('priority','?')}] {t.get('title')}" for t in tasks[:10])
            return str(tasks)

        elif tool == "mantis_get_health":
            r = httpx.get(f"{base}/api/health-data", headers=headers, timeout=10)
            d = r.json() if r.status_code == 200 else {}
            if isinstance(d, list) and d:
                d = d[0]
            return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)

        else:
            return f"Unbekanntes Tool: {tool}"
    except Exception as e:
        return f"Fehler: {e}"


def run_stdio() -> None:
    """MCP stdio-Modus: liest JSON-RPC von stdin, schreibt Antworten auf stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            method = req.get("method", "")
            rid = req.get("id")

            if method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": MCP_TOOLS}}
            elif method == "tools/call":
                params = req.get("params", {})
                tool = params.get("name", "")
                args = params.get("arguments", {})
                result = _handle_mcp_call(tool, args)
                resp = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": result}]}}
            elif method == "initialize":
                resp = {"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mantis-mcp", "version": "1.0.0"},
                }}
            else:
                resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}

            print(json.dumps(resp), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    run_stdio()
