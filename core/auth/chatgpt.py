"""OAuth-Hilfen für die lokale ChatGPT-Plan-Integration."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTHORIZATION_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DIRECT_TOKEN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_TOKEN_SCOPE
REGISTRATION_CLIENT_ID = "dynamic_agent_client"
CALLBACK_PATH = "/auth/callback"
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")
_SAFE_CODE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    nonce: str
    code_verifier: str


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def build_authorization_request(
    *,
    client_id: str,
    redirect_uri: str,
    ext_agent_host_id: str,
    first_registration: bool,
) -> AuthorizationRequest:
    """Baut die Authorization-URL mit frischem State, Nonce und PKCE S256."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if first_registration:
        params["ext_agent_host_id"] = ext_agent_host_id
        params["agent_name_hint"] = "Mantis"
    return AuthorizationRequest(f"{AUTHORIZATION_URL}?{urlencode(params)}", state, nonce, verifier)


def _query_value(query: dict, key: str) -> str:
    value = query.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "")


def validate_callback(
    query: dict,
    expected_state: str,
    *,
    require_client_id: bool = False,
) -> dict[str, str]:
    """Validiert State und liest den kurzlebigen Authorization-Code aus."""
    returned_state = _query_value(query, "state")
    if not returned_state or not hmac.compare_digest(returned_state, expected_state):
        raise ValueError("OAuth-Callback hat einen ungültigen state")
    error = _query_value(query, "error")
    if error:
        safe_error = _SAFE_CODE.sub("_", error)[:100]
        raise ValueError(f"OAuth-Anmeldung abgelehnt ({safe_error})")
    code = _query_value(query, "code")
    if not code:
        raise ValueError("OAuth-Callback enthält keinen Authorization-Code")
    client_id = _query_value(query, "client_id")
    if require_client_id and not client_id:
        raise ValueError("OAuth-Callback enthält keine ausgestellte Client-ID")
    return {"code": code, "client_id": client_id}


def exchange_authorization_code(
    client: httpx.Client,
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    """Tauscht den Code form-encoded ein; der Aufrufer übergibt die ausgestellte Client-ID."""
    response = client.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    })
    if response.is_error:
        raise RuntimeError(f"ChatGPT-Token-Austausch fehlgeschlagen (HTTP {response.status_code})")
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ChatGPT-Token-Austausch lieferte ungültiges JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("ChatGPT-Token-Austausch lieferte ein ungültiges Ergebnis")
    return data


def require_direct_token_scope(scope: str | list[str] | None) -> None:
    """Bricht die Anmeldung ab, wenn die direkte Inferenzberechtigung fehlt."""
    granted = set(scope.split() if isinstance(scope, str) else (scope or []))
    if DIRECT_TOKEN_SCOPE not in granted:
        raise ValueError(f"OAuth-Berechtigung {DIRECT_TOKEN_SCOPE} fehlt")


def fetch_jwks(client: httpx.Client) -> dict:
    """Liest die JWKS-Adresse aus dem standardisierten OIDC-Discovery-Dokument.

    Annahme: Der Issuer veröffentlicht die JWKS-URI über OIDC Discovery; sie wird
    nicht fest im Code verdrahtet und muss auf auth.openai.com zeigen.
    """
    discovery_response = client.get(DISCOVERY_URL)
    if discovery_response.is_error:
        raise RuntimeError(f"OIDC-Discovery fehlgeschlagen (HTTP {discovery_response.status_code})")
    try:
        discovery = discovery_response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("OIDC-Discovery lieferte ungültiges JSON") from exc
    jwks_uri = discovery.get("jwks_uri") if isinstance(discovery, dict) else None
    if not isinstance(jwks_uri, str) or not jwks_uri.startswith(f"{ISSUER}/"):
        raise RuntimeError("OIDC-Discovery enthält keine vertrauenswürdige JWKS-Adresse")
    response = client.get(jwks_uri)
    if response.is_error:
        raise RuntimeError(f"JWKS-Abruf fehlgeschlagen (HTTP {response.status_code})")
    try:
        jwks = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("JWKS-Antwort enthält ungültiges JSON") from exc
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise RuntimeError("JWKS-Antwort enthält keine Schlüssel")
    return jwks


def _verify_rs256(signing_input: bytes, signature: bytes, jwk: dict) -> bool:
    try:
        if jwk.get("kty") != "RSA":
            return False
        if jwk.get("use") not in (None, "sig") or jwk.get("alg") not in (None, "RS256"):
            return False
        modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        if modulus.bit_length() < 2048 or modulus.bit_length() > 8192 or exponent < 3 or exponent % 2 == 0:
            return False
        size = (modulus.bit_length() + 7) // 8
        if len(signature) != size:
            return False
        encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(size, "big")
        digest_info = _DIGEST_INFO_SHA256 + hashlib.sha256(signing_input).digest()
        padding_size = size - len(digest_info) - 3
        if padding_size < 8:
            return False
        expected = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
        return hmac.compare_digest(encoded, expected)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def validate_id_token(
    token: str,
    *,
    client_id: str,
    nonce: str,
    jwks: dict,
    now: float | None = None,
) -> dict:
    """Verifiziert RS256 und die verpflichtenden OIDC-Claims des Login-Tokens."""
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_b64url_decode(header_part))
        claims = json.loads(_b64url_decode(payload_part))
        signature = _b64url_decode(signature_part)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("ID-Token ist ungültig") from exc
    if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict):
        raise ValueError("ID-Token verwendet keinen unterstützten Signaturalgorithmus")
    keys = jwks.get("keys", []) if isinstance(jwks, dict) else []
    key = next((item for item in keys if isinstance(item, dict) and item.get("kid") == header.get("kid")), None)
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    if key is None or not _verify_rs256(signing_input, signature, key):
        raise ValueError("ID-Token-Signatur ist ungültig")

    if claims.get("iss") != ISSUER:
        raise ValueError("ID-Token hat einen ungültigen issuer")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or client_id not in audiences:
        raise ValueError("ID-Token hat eine ungültige audience")
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise ValueError("ID-Token hat einen ungültigen authorized party claim")
    expires_at = claims.get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("ID-Token hat kein gültiges Ablaufdatum")
    current_time = time.time() if now is None else now
    if expires_at <= current_time:
        raise ValueError("ID-Token ist abgelaufen")
    not_before = claims.get("nbf")
    if isinstance(not_before, (int, float)) and not isinstance(not_before, bool) and not_before > current_time:
        raise ValueError("ID-Token ist noch nicht gültig")
    returned_nonce = claims.get("nonce")
    if not isinstance(returned_nonce, str) or not hmac.compare_digest(returned_nonce, nonce):
        raise ValueError("ID-Token hat eine ungültige nonce")
    return claims


def _register_tokens(data: dict) -> None:
    try:
        from core.error_redaction import register_sensitive_values
        register_sensitive_values(data.get("access_token"), data.get("refresh_token"), data.get("id_token"))
    except Exception:
        pass


def register_token_secrets(*values) -> None:
    """Macht kurzlebige OAuth-Werte auch vor dem Speichern schwärzbar."""
    try:
        from core.error_redaction import register_sensitive_values
        register_sensitive_values(*values)
    except Exception:
        pass


def save_auth(path: str | Path, data: dict) -> None:
    """Schreibt OAuth-Daten via fsync + os.replace mit Verzeichnis 0700 und Datei 0600."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    _register_tokens(data)


def load_auth(path: str | Path) -> dict:
    """Liest und prüft die lokale Auth-Datei, ohne ihren Inhalt auszugeben."""
    try:
        with Path(path).expanduser().open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ChatGPT-Login-Datei fehlt oder ist ungültig") from exc
    required = ("client_id", "ext_agent_host_id", "access_token", "refresh_token", "expires_at", "scope")
    if not isinstance(data, dict) or any(not data.get(key) for key in required):
        raise ValueError("ChatGPT-Login-Datei enthält unvollständige Daten")
    try:
        expires_at = float(data["expires_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("ChatGPT-Login-Datei enthält ein ungültiges Ablaufdatum") from exc
    if not math.isfinite(expires_at) or expires_at <= 0:
        raise ValueError("ChatGPT-Login-Datei enthält ein ungültiges Ablaufdatum")
    _register_tokens(data)
    return data


def is_auth_configured(path: str | Path) -> bool:
    """Gibt nur True für eine vollständige Anmeldung mit direkter Inferenzberechtigung zurück."""
    try:
        data = load_auth(path)
        if not str(data.get("client_id", "")).startswith("oaiapp_"):
            return False
        require_direct_token_scope(data.get("scope"))
        return True
    except (OSError, ValueError, TypeError):
        return False
