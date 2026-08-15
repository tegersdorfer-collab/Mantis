"""Tests für den COROS-OAuth-Baustein (domains/coros/oauth.py).

Nur die reinen Anteile: PKCE, Refresh-Entscheidung, URL-Bau, Token-Datei.
Der Browser-Flow selbst wird nicht automatisiert getestet.
"""

import base64
import hashlib
import os
import sys
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains.coros import oauth


META = {
    "issuer": "https://mcpeu.coros.com",
    "authorization_endpoint": "https://mcpeu.coros.com/oauth2/authorize",
    "token_endpoint": "https://mcpeu.coros.com/oauth2/token",
    "registration_endpoint": "https://mcpeu.coros.com/connect/register",
}


class _FakeResponse:
    """Ersatz für httpx.Response, um oauth.httpx.post ohne echtes Netzwerk zu
    monkeypatchen — nur .raise_for_status() und .json() werden gebraucht."""

    def __init__(self, json_body: dict, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.invalid/token")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Fake-Fehler", request=request, response=response)

    def json(self):
        return self._json


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


def test_authorize_url_carries_resource_from_configured_mcp_url(monkeypatch):
    """RFC 8707: die Autorisierungsanfrage muss die kanonische Resource-URI tragen,
    abgeleitet von COROS_MCP_URL — nicht hart codiert, damit ein Regions-Wechsel
    (EU/US/CN) automatisch die richtige URI liefert."""
    monkeypatch.setattr(oauth.config, "COROS_MCP_URL", "https://mcpus.coros.com/mcp")
    url = oauth.build_authorize_url(META, "cid-1", "chal-1", "state-1")
    q = parse_qs(urlparse(url).query)
    assert q["resource"] == ["https://mcpus.coros.com"]


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


# ── refresh(): der Baustein, der Mantis monatelang unbeaufsichtigt verbunden hält ──

def test_refresh_carries_forward_old_refresh_token_when_server_omits_it(monkeypatch):
    def fake_post(url, data=None, timeout=None, headers=None):
        assert url == META["token_endpoint"]
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "alt-rt"
        return _FakeResponse({"access_token": "neu-at", "expires_in": 3600})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    tok = oauth.refresh(META, "cid", "alt-rt")
    assert tok["access_token"] == "neu-at"
    assert tok["refresh_token"] == "alt-rt"


def test_refresh_uses_new_refresh_token_when_server_sends_one(monkeypatch):
    def fake_post(url, data=None, timeout=None, headers=None):
        return _FakeResponse({"access_token": "neu-at", "refresh_token": "neu-rt",
                              "expires_in": 3600})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    tok = oauth.refresh(META, "cid", "alt-rt")
    assert tok["refresh_token"] == "neu-rt"


# ── access_token(): Refresh-und-Persistieren-Zweig + Fehlerübersetzung ──

def _write_expired_token(tmp_path, monkeypatch):
    path = tmp_path / "coros_token.json"
    monkeypatch.setattr(oauth, "TOKEN_PATH", path)
    tok = {"client_id": "cid", "access_token": "alt-at", "refresh_token": "alt-rt",
           "expires_at": 0.0, "issuer": META["issuer"],
           "token_endpoint": META["token_endpoint"]}
    oauth.save_token(tok)
    return path


def test_access_token_refreshes_and_persists_when_expired(tmp_path, monkeypatch):
    _write_expired_token(tmp_path, monkeypatch)

    def fake_post(url, data=None, timeout=None, headers=None):
        return _FakeResponse({"access_token": "neu-at", "refresh_token": "neu-rt",
                              "expires_in": 3600})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    at = oauth.access_token()
    assert at == "neu-at"
    persisted = oauth.load_token()
    assert persisted["access_token"] == "neu-at"
    assert persisted["refresh_token"] == "neu-rt"


def test_access_token_translates_http_error_to_not_authorized(tmp_path, monkeypatch):
    _write_expired_token(tmp_path, monkeypatch)

    def fake_post(url, data=None, timeout=None, headers=None):
        return _FakeResponse({}, status_code=400)

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    try:
        oauth.access_token()
    except oauth.CorosNotAuthorized as e:
        assert "coros_auth" in str(e)
    else:
        raise AssertionError("CorosNotAuthorized erwartet")


# ── exchange_code() / _to_token(): Tausch des Codes und legible Fehlerbehandlung ──

def test_exchange_code_builds_token_and_sends_resource(monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None, headers=None):
        captured["data"] = data
        return _FakeResponse({"access_token": "at", "refresh_token": "rt",
                              "expires_in": 3600})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    tok = oauth.exchange_code(META, "cid", "der-code", "verifier-x")
    assert tok["access_token"] == "at"
    assert tok["client_id"] == "cid"
    assert tok["issuer"] == META["issuer"]
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "der-code"
    assert captured["data"]["resource"] == "https://mcpeu.coros.com"


def test_to_token_raises_legibly_instead_of_bare_keyerror(monkeypatch):
    def fake_post(url, data=None, timeout=None, headers=None):
        return _FakeResponse({"error": "invalid_grant", "error_description": "abgelaufen"})

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    try:
        oauth.exchange_code(META, "cid", "der-code", "verifier-x")
    except oauth.CorosNotAuthorized as e:
        msg = str(e)
        assert "error" in msg  # nennt die Feldnamen …
        assert "invalid_grant" not in msg  # … aber nicht den Body selbst
    else:
        raise AssertionError("CorosNotAuthorized erwartet, kein KeyError")
