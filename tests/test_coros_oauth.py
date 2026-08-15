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
