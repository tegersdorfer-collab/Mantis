# COROS MCP — Phase A: Zugang und Erkundung

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mantis kann sich beim COROS-MCP authentifizieren, Tools aufrufen und legt die echten Antwortformate als Fixtures ab.

**Architecture:** `domains/coros/oauth.py` hält Registrierung, PKCE-Flow und Token-Refresh; `domains/coros/client.py` spricht MCP-Streamable-HTTP über den vorhandenen `httpx`. Beide sind reine Bibliotheks-Module ohne Seiteneffekte beim Import. Zwei Skripte darüber: `coros_auth.py` (einmaliger Login durch Timo) und `coros_probe.py` (dumpt Tool-Schemata und Beispiel-Antworten).

**Tech Stack:** Python 3.14.3, httpx (vorhanden), pytest. **Keine neue Dependency.**

**Spec:** [docs/superpowers/specs/2026-08-15-coros-mcp-health-sync-design.md](../specs/2026-08-15-coros-mcp-health-sync-design.md)

## Global Constraints

- Kein neues Paket in `requirements.txt`. Das offizielle `mcp`-SDK (2.0.0) zieht `httpx2`, `starlette`, `uvicorn`, `sse-starlette`, `opentelemetry-api` und `python-multipart` nach — ein zweiter HTTP-Stack neben dem vorhandenen `httpx` für einen reinen Client-Anwendungsfall. Die Spec erlaubt diesen Rückfall ausdrücklich; er wird hier zur Vorgabe.
- MCP-Endpoint: `https://mcpeu.coros.com/mcp` (Default, per `COROS_MCP_URL` überschreibbar).
- OAuth-Scopes exakt: `openid mcp.tools offline_access`.
- Redirect-URI exakt: `http://127.0.0.1:8083/callback` (8081/8082 gehören gcal/gmail).
- MCP-Protokollversion: `2025-06-18`.
- Token-File: `data/coros_token.json`, Dateirechte `0600`. `data/` ist gitignored.
- Kommentare und Log-Ausgaben auf Deutsch, wie im übrigen Repo.
- Tests: pytest, `sys.path`-Insert am Dateikopf wie in `tests/test_health_mapping.py`. Netzwerk wird über `httpx.MockTransport` gefälscht, nie echt aufgerufen.
- **Diese Phase ändert den bestehenden Health-Pfad nicht.** Kein Abriss, keine Naht, keine Migration. Das ist Phase B/C.

---

### Task 1: OAuth-Grundlagen — PKCE, Discovery, Token-Store

**Files:**
- Create: `domains/coros/__init__.py`
- Create: `domains/coros/oauth.py`
- Create: `tests/test_coros_oauth.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `CorosNotAuthorized(RuntimeError)`
  - `pkce_pair() -> tuple[str, str]` — `(verifier, challenge)`
  - `discover(issuer: str) -> dict` — Metadaten vom Authorization Server
  - `register_client(meta: dict) -> str` — `client_id` via DCR
  - `build_authorize_url(meta: dict, client_id: str, challenge: str, state: str) -> str`
  - `exchange_code(meta: dict, client_id: str, code: str, verifier: str) -> dict`
  - `refresh(meta: dict, client_id: str, refresh_token: str) -> dict`
  - `save_token(tok: dict) -> None` / `load_token() -> dict | None`
  - `needs_refresh(tok: dict, now: float | None = None, skew: int = 60) -> bool`
  - `access_token(force_refresh: bool = False) -> str` — wirft `CorosNotAuthorized` ohne Token-File

- [ ] **Step 1: Test-Datei mit den reinen Funktionen schreiben**

`tests/test_coros_oauth.py`:

```python
"""Tests für den COROS-OAuth-Baustein (domains/coros/oauth.py).

Nur die reinen Anteile: PKCE, Refresh-Entscheidung, URL-Bau, Token-Datei.
Der Browser-Flow selbst wird nicht automatisiert getestet.
"""

import base64
import hashlib
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import oauth


META = {
    "issuer": "https://mcpeu.coros.com",
    "authorization_endpoint": "https://mcpeu.coros.com/oauth2/authorize",
    "token_endpoint": "https://mcpeu.coros.com/oauth2/token",
    "registration_endpoint": "https://mcpeu.coros.com/connect/register",
}


def test_pkce_challenge_is_sha256_of_verifier():
    verifier, challenge = oauth.pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected


def test_pkce_has_no_padding_and_enough_entropy():
    verifier, challenge = oauth.pkce_pair()
    assert "=" not in verifier and "=" not in challenge
    assert 43 <= len(verifier) <= 128


def test_pkce_pair_is_fresh_each_call():
    assert oauth.pkce_pair()[0] != oauth.pkce_pair()[0]


def test_authorize_url_carries_all_required_params():
    url = oauth.build_authorize_url(META, "cid-1", "chal-1", "state-1")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(META["authorization_endpoint"])
    assert q["client_id"] == ["cid-1"]
    assert q["code_challenge"] == ["chal-1"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["state-1"]
    assert q["redirect_uri"] == [oauth.REDIRECT_URI]
    assert q["scope"] == [oauth.SCOPES]


def test_needs_refresh_true_when_no_token():
    assert oauth.needs_refresh({}) is True


def test_needs_refresh_true_inside_skew_window():
    assert oauth.needs_refresh({"access_token": "a", "expires_at": 1000.0}, now=959.0) is True


def test_needs_refresh_false_when_comfortably_valid():
    assert oauth.needs_refresh({"access_token": "a", "expires_at": 1000.0}, now=800.0) is False


def test_token_roundtrip_and_file_mode(tmp_path, monkeypatch):
    path = tmp_path / "coros_token.json"
    monkeypatch.setattr(oauth, "TOKEN_PATH", path)
    tok = {"client_id": "cid", "access_token": "at", "refresh_token": "rt",
           "expires_at": 123.0, "issuer": META["issuer"]}
    oauth.save_token(tok)
    assert oauth.load_token() == tok
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_load_token_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "TOKEN_PATH", tmp_path / "nichts.json")
    assert oauth.load_token() is None


def test_access_token_raises_without_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "TOKEN_PATH", tmp_path / "nichts.json")
    try:
        oauth.access_token()
    except oauth.CorosNotAuthorized as e:
        assert "coros_auth" in str(e)
    else:
        raise AssertionError("CorosNotAuthorized erwartet")
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_oauth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'domains.coros'`

- [ ] **Step 3: Modul anlegen**

`domains/coros/__init__.py`:

```python
"""COROS-Anbindung über deren offizielles MCP (https://mcpeu.coros.com/mcp)."""
```

`domains/coros/oauth.py`:

```python
"""COROS-OAuth — Dynamic Client Registration + Authorization Code mit PKCE.

Public Client (kein Secret): COROS erlaubt `token_endpoint_auth_method: none`,
die Sicherheit hängt an PKCE. Token liegt in data/coros_token.json, spiegelt das
gcal/gmail-Muster (eigenes Token-File, eigener Callback-Port).

Der Browser-Login läuft einmalig über scripts/coros_auth.py; danach hält der
Refresh-Token die Verbindung.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger("mantis.coros")

ROOT = Path(__file__).parent.parent.parent
TOKEN_PATH = ROOT / "data" / "coros_token.json"

REDIRECT_URI = "http://127.0.0.1:8083/callback"
SCOPES = "openid mcp.tools offline_access"
CLIENT_NAME = "Mantis"


class CorosNotAuthorized(RuntimeError):
    """Kein oder kein brauchbares Token — Timo muss scripts/coros_auth.py laufen lassen."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) nach RFC 7636, Methode S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def discover(issuer: str) -> dict:
    """Authorization-Server-Metadaten holen (RFC 8414)."""
    url = issuer.rstrip("/") + "/.well-known/oauth-authorization-server"
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def register_client(meta: dict) -> str:
    """Sich selbst als Public Client registrieren (RFC 7591) → client_id."""
    resp = httpx.post(meta["registration_endpoint"], timeout=15, json={
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPES,
    })
    resp.raise_for_status()
    return resp.json()["client_id"]


def build_authorize_url(meta: dict, client_id: str, challenge: str, state: str) -> str:
    return meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def _token_request(meta: dict, data: dict) -> dict:
    resp = httpx.post(meta["token_endpoint"], data=data, timeout=20,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    return resp.json()


def exchange_code(meta: dict, client_id: str, code: str, verifier: str) -> dict:
    raw = _token_request(meta, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    return _to_token(raw, meta, client_id)


def refresh(meta: dict, client_id: str, refresh_token: str) -> dict:
    raw = _token_request(meta, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })
    tok = _to_token(raw, meta, client_id)
    # Manche Server geben beim Refresh keinen neuen Refresh-Token zurück.
    tok["refresh_token"] = raw.get("refresh_token") or refresh_token
    return tok


def _to_token(raw: dict, meta: dict, client_id: str) -> dict:
    return {
        "client_id": client_id,
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token", ""),
        "expires_at": time.time() + float(raw.get("expires_in", 3600)),
        "issuer": meta["issuer"],
        "token_endpoint": meta["token_endpoint"],
    }


def save_token(tok: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"COROS-Token unlesbar ({e}) — bitte scripts/coros_auth.py erneut laufen lassen")
        return None


def needs_refresh(tok: dict, now: float | None = None, skew: int = 60) -> bool:
    if not tok or not tok.get("access_token"):
        return True
    return (now if now is not None else time.time()) >= float(tok.get("expires_at", 0)) - skew


def access_token(force_refresh: bool = False) -> str:
    """Gültigen Bearer liefern; refresht bei Bedarf und schreibt das Token zurück."""
    tok = load_token()
    if not tok:
        raise CorosNotAuthorized(
            "COROS nicht autorisiert — bitte einmalig `python3 scripts/coros_auth.py` ausführen."
        )
    if force_refresh or needs_refresh(tok):
        if not tok.get("refresh_token"):
            raise CorosNotAuthorized(
                "COROS-Token abgelaufen und kein Refresh-Token da — bitte "
                "`python3 scripts/coros_auth.py` erneut ausführen."
            )
        meta = {"issuer": tok["issuer"], "token_endpoint": tok["token_endpoint"]}
        try:
            tok = refresh(meta, tok["client_id"], tok["refresh_token"])
        except httpx.HTTPStatusError as e:
            raise CorosNotAuthorized(
                f"COROS-Refresh abgelehnt ({e.response.status_code}) — bitte "
                "`python3 scripts/coros_auth.py` erneut ausführen."
            ) from e
        save_token(tok)
    return tok["access_token"]
```

- [ ] **Step 4: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_oauth.py -q`
Expected: PASS, 10 Tests

- [ ] **Step 5: Committen**

```bash
git add domains/coros/__init__.py domains/coros/oauth.py tests/test_coros_oauth.py
git commit -m "feat(coros): OAuth-Baustein mit DCR, PKCE und Token-Store"
```

---

### Task 2: Login-Skript

**Files:**
- Create: `scripts/coros_auth.py`

**Interfaces:**
- Consumes: `domains.coros.oauth` (alle Funktionen aus Task 1)
- Produces: `data/coros_token.json` — die Datei, von der Task 3 lebt

Dieses Skript wird nicht automatisiert getestet: es öffnet einen Browser und wartet auf einen
menschlichen Login. Die Prüfung ist der Lauf durch Timo in Step 3.

- [ ] **Step 1: Skript schreiben**

`scripts/coros_auth.py`:

```python
#!/usr/bin/env python3
"""
Einmaliger OAuth-Login für Mantis → COROS-MCP.

Ausführen:
  cd /Users/timoegersdorfer/Mantis
  python3 scripts/coros_auth.py

Registriert Mantis per Dynamic Client Registration bei COROS, öffnet den Login
im Browser und speichert data/coros_token.json. Danach hält der Refresh-Token
die Verbindung; das Skript muss nur bei Entzug oder Ablauf erneut laufen.
"""
import http.server
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import config
from domains.coros import oauth

PORT = 8083


def main():
    issuer = config.COROS_MCP_URL.rsplit("/mcp", 1)[0]
    print(f"🔎 Metadaten von {issuer} …")
    meta = oauth.discover(issuer)

    tok = oauth.load_token()
    client_id = tok["client_id"] if tok and tok.get("client_id") else oauth.register_client(meta)
    print(f"🪪 Client-ID: {client_id}")

    verifier, challenge = oauth.pkce_pair()
    state = secrets.token_urlsafe(16)
    url = oauth.build_authorize_url(meta, client_id, challenge, state)

    print("\n🌐 Öffne diesen Link und melde dich mit deinem COROS-Account an:\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in params.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = ("<h2>✅ COROS verbunden.</h2><p>Fenster kann zu.</p>" if ok
                    else "<h2>❌ Fehlgeschlagen.</h2><p>Siehe Terminal.</p>")
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *_):
            pass

    print(f"⏳ Warte auf den Callback auf Port {PORT} …")
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as srv:
        srv.handle_request()

    if result.get("state") != state:
        print(f"❌ State stimmt nicht — Abbruch. Antwort: {result}")
        sys.exit(1)
    if "code" not in result:
        print(f"❌ Kein Code zurückgekommen: {result}")
        sys.exit(1)

    tok = oauth.exchange_code(meta, client_id, result["code"], verifier)
    oauth.save_token(tok)
    print(f"✅ Token gespeichert in {oauth.TOKEN_PATH}")
    print("   Nächster Schritt: python3 scripts/coros_probe.py")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: `COROS_MCP_URL` in die Konfiguration aufnehmen**

In `settings.py`, im selben Stil wie die umliegenden Blöcke, einen Abschnitt ergänzen:

```python
    # ── COROS ────────────────────────────────────────────────────────────────
    # Offizielles COROS-MCP. EU-Route als Default; mcpus/mcpcn falls der Account
    # dort liegt. Siehe docs/coros-setup.md.
    COROS_MCP_URL: str = "https://mcpeu.coros.com/mcp"
```

In `config.py` bei den übrigen Durchreichungen:

```python
COROS_MCP_URL            = cfg.COROS_MCP_URL
```

- [ ] **Step 3: Syntax prüfen und den Login durch Timo laufen lassen**

Run: `cd ~/Mantis && python3 -c "import ast,pathlib; ast.parse(pathlib.Path('scripts/coros_auth.py').read_text()); print('ok')"`
Expected: `ok`

Dann **Timo** ausführen lassen — nicht der Agent, der Login verlangt Zugangsdaten:

```bash
cd ~/Mantis && python3 scripts/coros_auth.py
```

Expected: Browser öffnet die COROS-Anmeldung, nach dem Zustimmen erscheint
"✅ COROS verbunden.", und `data/coros_token.json` existiert mit `access_token`
und `refresh_token`.

Falls die Autorisierungsseite meldet, der Account liege in einer anderen Region:
`COROS_MCP_URL=https://mcpus.coros.com/mcp` in die `.env` schreiben und das Skript
erneut starten.

- [ ] **Step 4: Committen**

```bash
git add scripts/coros_auth.py settings.py config.py
git commit -m "feat(coros): Login-Skript und COROS_MCP_URL-Konfiguration"
```

---

### Task 3: MCP-Client über httpx

**Files:**
- Create: `domains/coros/client.py`
- Create: `tests/test_coros_client.py`

**Interfaces:**
- Consumes: `oauth.access_token(force_refresh=False) -> str`, `oauth.CorosNotAuthorized`
- Produces:
  - `CorosToolError(RuntimeError)`
  - `CorosClient(base_url: str | None = None, http: httpx.Client | None = None, token_provider=oauth.access_token)`
  - `CorosClient.list_tools() -> list[dict]` — die `tools`-Liste aus `tools/list`
  - `CorosClient.call_tool(name: str, arguments: dict) -> dict` — das `result`-Objekt
  - `payload(result: dict) -> dict | list | str` — holt `structuredContent`, sonst den geparsten Text

Hintergrund für den Umsetzenden: MCP über Streamable HTTP ist JSON-RPC 2.0 per POST auf
einen einzigen Endpoint. Der Server antwortet entweder `application/json` oder
`text/event-stream` mit `data:`-Zeilen — beides muss der Client vertragen. Nach
`initialize` gibt der Server optional einen `Mcp-Session-Id`-Header zurück, den alle
weiteren Anfragen mitschicken müssen, und der Client muss einmal
`notifications/initialized` senden, bevor er Tools aufruft.

- [ ] **Step 1: Test-Datei schreiben**

`tests/test_coros_client.py`:

```python
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
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'domains.coros.client'`

- [ ] **Step 3: Client schreiben**

`domains/coros/client.py`:

```python
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
```

- [ ] **Step 4: Tests laufen lassen, grün bestätigen**

Run: `cd ~/Mantis && python3 -m pytest tests/test_coros_client.py -q`
Expected: PASS, 10 Tests

- [ ] **Step 5: Gesamtsuite laufen lassen**

Run: `cd ~/Mantis && python3 -m pytest -q`
Expected: keine neuen Fehlschläge gegenüber dem Stand vor dieser Aufgabe

- [ ] **Step 6: Committen**

```bash
git add domains/coros/client.py tests/test_coros_client.py
git commit -m "feat(coros): MCP-Client über httpx, ohne SDK-Abhängigkeit"
```

---

### Task 4: Erkundungs-Skript und Fixtures

**Files:**
- Create: `scripts/coros_probe.py`
- Create: `docs/coros-setup.md`
- Create: `tests/fixtures/coros/` (Inhalt entsteht beim Lauf)

**Interfaces:**
- Consumes: `CorosClient.list_tools()`, `CorosClient.call_tool(name, arguments)`, `payload(result)`
- Produces: `tests/fixtures/coros/_tools.json` (Katalog inkl. `inputSchema`) und je Tool eine
  `tests/fixtures/coros/<toolName>.json` mit den gesendeten Argumenten und der rohen Antwort.
  **Das sind die Eingangsdaten für Plan B.**

Das Skript rät keine Parameternamen: es liest sie aus dem `inputSchema`, das `tools/list`
mitliefert, und füllt datumsartige Pflichtfelder heuristisch. Was es gesendet hat, steht in
jeder Fixture mit drin — falls ein Aufruf leer zurückkommt, ist nachvollziehbar warum.

- [ ] **Step 1: Skript schreiben**

`scripts/coros_probe.py`:

```python
#!/usr/bin/env python3
"""
Erkundet das COROS-MCP: dumpt den Tool-Katalog und je eine Beispiel-Antwort.

Ausführen (nach scripts/coros_auth.py):
  cd /Users/timoegersdorfer/Mantis
  python3 scripts/coros_probe.py

Schreibt nach tests/fixtures/coros/. Die Feldnamen der COROS-Antworten sind
nirgends dokumentiert — diese Dateien sind die Grundlage für das Mapping in
Phase B. Reines Lese-Werkzeug, ändert nichts an Mantis.
"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from domains.coros import oauth
from domains.coros.client import CorosClient

OUT = ROOT / "tests" / "fixtures" / "coros"

# Nur lesende Tools. downloadActivityFitFiles bleibt bewusst draußen (Limit 50/Tag).
WANTED = [
    "queryUserInfo", "queryDevices",
    "queryDailyHealthData", "querySleepData", "querySleepHrv",
    "queryRestingHeartRate", "queryAvgHeartRate", "queryStressLevel",
    "queryRecoveryStatus", "queryTrainingLoadAssessment",
    "queryFitnessAssessmentOverview", "querySportRecords",
    "queryTrainingSchedule",
]

TODAY = date.today()
START = TODAY - timedelta(days=7)


def guess_args(schema: dict) -> dict:
    """Pflichtfelder aus dem inputSchema heuristisch füllen.

    Datumsartige Namen bekommen ISO-Daten der letzten Woche, Zahlen eine 1,
    Booleans False. Alles Geratene steht in der Fixture, ist also überprüfbar.
    """
    props = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    args = {}
    for name in required:
        spec = props.get(name, {})
        typ = spec.get("type", "string")
        low = name.lower()
        if typ in ("integer", "number"):
            # COROS nutzt an manchen Stellen YYYYMMDD als Zahl statt ISO-String.
            args[name] = int(TODAY.strftime("%Y%m%d")) if "date" in low or "day" in low else 1
        elif typ == "boolean":
            args[name] = False
        elif "start" in low or "from" in low or "begin" in low:
            args[name] = START.isoformat()
        elif "end" in low or "to" in low or "until" in low:
            args[name] = TODAY.isoformat()
        elif "date" in low or "day" in low:
            args[name] = TODAY.isoformat()
        else:
            args[name] = ""
    return args


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        oauth.access_token()
    except oauth.CorosNotAuthorized as e:
        print(f"❌ {e}")
        sys.exit(1)

    with CorosClient() as c:
        tools = c.list_tools()
        (OUT / "_tools.json").write_text(
            json.dumps(tools, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"📚 {len(tools)} Tools im Katalog → tests/fixtures/coros/_tools.json")

        by_name = {t["name"]: t for t in tools}
        unknown = [n for n in WANTED if n not in by_name]
        if unknown:
            print(f"⚠️  Nicht im Katalog (Doku veraltet?): {', '.join(unknown)}")

        ok = fail = 0
        for name in WANTED:
            tool = by_name.get(name)
            if not tool:
                continue
            args = guess_args(tool.get("inputSchema"))
            try:
                result = c.call_tool(name, args)
                record = {"tool": name, "arguments": args, "result": result}
                ok += 1
                print(f"  ✅ {name}  args={args}")
            except Exception as e:
                record = {"tool": name, "arguments": args, "error": f"{type(e).__name__}: {e}"}
                fail += 1
                print(f"  ❌ {name}  args={args}  → {e}")
            (OUT / f"{name}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            time.sleep(1)  # höflich bleiben

    print(f"\n{ok} erfolgreich, {fail} fehlgeschlagen. Dateien in {OUT}")
    if fail:
        print("Bei Fehlschlägen: das inputSchema in _tools.json ansehen und die "
              "Argumente in WANTED/guess_args nachziehen.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Syntax prüfen**

Run: `cd ~/Mantis && python3 -c "import ast,pathlib; ast.parse(pathlib.Path('scripts/coros_probe.py').read_text()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Erkundung laufen lassen**

Voraussetzung: Task 2 Step 3 ist durch, `data/coros_token.json` existiert.

Run: `cd ~/Mantis && python3 scripts/coros_probe.py`
Expected: Katalog mit rund 20 Tools, und für die meisten der 13 gewünschten Tools eine
Antwort. Fehlschläge sind hier **kein Abbruch** — sie sind Erkenntnis: das `inputSchema`
in `_tools.json` verrät die richtigen Parameternamen, dann `guess_args` nachziehen und
erneut laufen lassen, bis mindestens `queryUserInfo`, `queryDailyHealthData`,
`querySleepData` und `querySportRecords` echte Daten liefern.

- [ ] **Step 4: Setup-Dokumentation schreiben**

`docs/coros-setup.md`, im Stil von `docs/fan-gmail-setup.md` (der äußere Zaun hier ist
vierfach, damit die inneren Shell-Blöcke drinbleiben — in die Datei kommen sie dreifach):

````markdown
# COROS-MCP einrichten

Mantis holt Gesundheits- und Trainingsdaten direkt aus der COROS-Cloud über deren
offizielles MCP. Ersetzt den alten Weg über Apple Health und die BodyOS-App.

## Einmalig

```bash
cd ~/Mantis
python3 scripts/coros_auth.py
```

Öffnet den COROS-Login im Browser. Nach der Zustimmung liegt das Token in
`data/coros_token.json` (Rechte 0600, gitignored). Der Refresh-Token hält die
Verbindung; das Skript muss nur erneut laufen, wenn du den Zugriff im
COROS-Account entziehst.

## Region

Default ist die EU-Route. Liegt der Account woanders, in die `.env`:

```
COROS_MCP_URL=https://mcpus.coros.com/mcp
```

Verfügbar sind `mcpeu`, `mcpus` und `mcpcn`.

## Nachsehen, was das MCP liefert

```bash
python3 scripts/coros_probe.py
```

Schreibt Tool-Katalog und Beispiel-Antworten nach `tests/fixtures/coros/`.

## Wenn es klemmt

| Symptom | Ursache |
|---|---|
| `COROS nicht autorisiert` | Kein Token-File — `scripts/coros_auth.py` laufen lassen |
| `COROS-Refresh abgelehnt` | Zugriff im COROS-Account entzogen — neu autorisieren |
| Alle Tools 401 | Falsche Region, siehe oben |
| Tool meldet fehlende Parameter | `inputSchema` in `tests/fixtures/coros/_tools.json` ansehen |
````

- [ ] **Step 5: Committen**

Die Fixtures kommen mit ins Repo — sie sind die Testgrundlage für Phase B. Vorher
**durchsehen und persönliche Werte prüfen**: `queryUserInfo` enthält Geburtsdatum,
Größe und Gewicht, `querySportRecords` enthält Startkoordinaten von Läufen. Was da nicht
hingehört, vor dem Commit durch Platzhalter ersetzen.

```bash
git add scripts/coros_probe.py docs/coros-setup.md tests/fixtures/coros/
git commit -m "feat(coros): Erkundungs-Skript, Fixtures und Setup-Doku"
```

---

## Abschluss der Phase

Danach steht: Mantis ist bei COROS autorisiert, kann Tools aufrufen, und die echten
Antwortformate liegen als Fixtures im Repo. Der bestehende Health-Pfad ist unberührt.

**Nächster Schritt:** Plan B (`docs/superpowers/plans/<datum>-coros-mcp-phase-b-import.md`)
wird gegen diese Fixtures geschrieben — `mapping.py` per TDD, dann `importer.py`, dann die
Naht in `dashboard.refresh_health()`, dann der Abriss des Push-Pfads. Er lässt sich erst
schreiben, wenn die Dateien aus Task 4 existieren; genau dafür ist der Schnitt hier.
