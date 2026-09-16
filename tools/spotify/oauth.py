"""Spotify-User-OAuth — Authorization Code für den lokalen Mantis-Dienst.

Mantis läuft als langlebiger Backend-Prozess auf dem eigenen Mac. Deshalb wird
der serverseitige Authorization-Code-Flow verwendet: Das Client-Secret bleibt
in der lokalen ``.env``, der Refresh-Token liegt geschützt unter ``data/``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from settings import cfg

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_URI = "http://127.0.0.1:8084/callback"
SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
TOKEN_PATH = Path(__file__).parent.parent.parent / "data" / "spotify_token.json"


class SpotifyOAuthError(RuntimeError):
    """Spotify-OAuth ist fehlgeschlagen oder nicht vollständig eingerichtet."""


class SpotifyNotAuthorized(SpotifyOAuthError):
    """Es existiert kein gültiger, erneuerbarer Spotify-User-Token."""


def credentials_missing() -> bool:
    return not (cfg.SPOTIFY_CLIENT_ID and cfg.SPOTIFY_CLIENT_SECRET)


def has_token() -> bool:
    """Liefert nur, ob bereits ein Token gespeichert wurde."""
    return bool(load_token())


def build_authorize_url(client_id: str | None = None, state: str = "") -> str:
    """Baut die Spotify-Login-URL für den lokalen Callback."""
    client_id = client_id or cfg.SPOTIFY_CLIENT_ID
    if not client_id:
        raise SpotifyNotAuthorized("SPOTIFY_CLIENT_ID fehlt in der .env.")
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })


def _token_request(data: dict) -> dict:
    """Fordert einen Token an; der Response-Body wird nicht in Fehlern ausgegeben."""
    if credentials_missing():
        raise SpotifyNotAuthorized(
            "SPOTIFY_CLIENT_ID und SPOTIFY_CLIENT_SECRET fehlen in der .env."
        )
    try:
        response = httpx.post(
            TOKEN_URL,
            data=data,
            auth=(cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET),
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise SpotifyOAuthError("Spotify-Token-Endpoint nicht erreichbar.") from exc
    if response.status_code != 200:
        raise SpotifyOAuthError(f"Spotify-Token-Abruf fehlgeschlagen (HTTP {response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise SpotifyOAuthError("Spotify-Token-Endpoint lieferte kein gültiges JSON.") from exc


def _normalise(raw: dict, old_refresh_token: str = "") -> dict:
    if not raw.get("access_token"):
        raise SpotifyOAuthError("Spotify-Token-Endpoint lieferte kein access_token.")
    return {
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token") or old_refresh_token,
        "expires_at": time.time() + float(raw.get("expires_in", 3600)),
        "scope": raw.get("scope", SCOPES),
    }


def exchange_code(code: str) -> dict:
    """Tauscht den einmaligen Authorization-Code gegen ein User-Token."""
    raw = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    return _normalise(raw)


def refresh(refresh_token: str) -> dict:
    """Erneuert das Access-Token und übernimmt ggf. den alten Refresh-Token."""
    raw = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    return _normalise(raw, old_refresh_token=refresh_token)


def save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        token = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpotifyNotAuthorized("Spotify-Token-Datei ist nicht lesbar.") from exc
    return token if isinstance(token, dict) else None


def needs_refresh(token: dict, now: float | None = None, skew: int = 60) -> bool:
    if not token or not token.get("access_token"):
        return True
    return (now if now is not None else time.time()) >= float(token.get("expires_at", 0)) - skew


def access_token(force_refresh: bool = False) -> str:
    """Liefert ein gültiges Token und refresht es bei Bedarf."""
    token = load_token()
    if not token or not token.get("access_token"):
        raise SpotifyNotAuthorized(
            "Spotify nicht autorisiert — bitte einmalig `python3 scripts/spotify_auth.py` ausführen."
        )
    if force_refresh or needs_refresh(token):
        if not token.get("refresh_token"):
            raise SpotifyNotAuthorized(
                "Spotify-Token abgelaufen und ohne Refresh-Token — bitte `python3 scripts/spotify_auth.py` erneut ausführen."
            )
        try:
            token = refresh(token["refresh_token"])
        except SpotifyOAuthError as exc:
            raise SpotifyNotAuthorized(
                "Spotify-Token konnte nicht erneuert werden — bitte `python3 scripts/spotify_auth.py` erneut ausführen."
            ) from exc
        save_token(token)
    return token["access_token"]
