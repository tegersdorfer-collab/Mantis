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
from urllib.parse import urlencode, urlsplit

import httpx

import config

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


def _resource() -> str:
    """Kanonische Resource-URI (RFC 8707) — Herkunft der konfigurierten COROS-MCP-URL,
    ohne Pfad. Aus der Konfiguration abgeleitet statt hart codiert, damit eine
    Regions-Umschaltung (COROS_MCP_URL) automatisch die richtige URI liefert."""
    parts = urlsplit(config.COROS_MCP_URL)
    return f"{parts.scheme}://{parts.netloc}"


def build_authorize_url(meta: dict, client_id: str, challenge: str, state: str) -> str:
    return meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": _resource(),
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
        "resource": _resource(),
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
