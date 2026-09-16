"""Spotify-Web-API — Suche und Playback über den authentifizierten User.

Für Playback wird ein Spotify-User-Token aus ``tools.spotify.oauth`` benötigt.
Die Suche fällt ohne User-Token weiterhin auf den bestehenden Client-Credentials-
Flow zurück, damit die bisherige Einrichtung während der Migration funktioniert.

Tests patchen die HTTP-Grenzen ``_http_post_token``, ``_http_get_search`` und
``_http_request``.
"""
from __future__ import annotations

import time

import httpx

from settings import cfg
from tools.spotify import oauth

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"
_API_URL = "https://api.spotify.com/v1"
_TYPES = ("track", "album", "playlist", "artist")  # zugleich Präferenz-Reihenfolge


class SpotifySearchError(RuntimeError):
    """Suche/Token-Abruf fehlgeschlagen (offline, falsche Credentials, …)."""


class SpotifyApiError(SpotifySearchError):
    """Ein authentifizierter Spotify-Web-API-Aufruf ist fehlgeschlagen."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


_token: str | None = None
_token_expires: float = 0.0


def credentials_missing() -> bool:
    return not (cfg.SPOTIFY_CLIENT_ID and cfg.SPOTIFY_CLIENT_SECRET)


async def _http_post_token() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(_TOKEN_URL, data={"grant_type": "client_credentials"},
                              auth=(cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET))
    if r.status_code != 200:
        raise SpotifySearchError(f"Token-Abruf fehlgeschlagen (HTTP {r.status_code})")
    return r.json()


async def _http_get_search(params: dict, token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(_SEARCH_URL, params=params,
                             headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise SpotifySearchError(f"Suche fehlgeschlagen (HTTP {r.status_code})")
    return r.json()


async def _http_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict | None:
    """Führt einen authentifizierten Spotify-API-Aufruf aus."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(
                method,
                _API_URL + path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as exc:
        raise SpotifyApiError("Spotify-API nicht erreichbar.") from exc
    if response.status_code == 204:
        return None
    if not 200 <= response.status_code < 300:
        message = ""
        try:
            payload = response.json()
            message = ((payload.get("error") or {}).get("message")
                       if isinstance(payload, dict) else "") or ""
        except ValueError:
            pass
        suffix = f": {message}" if message else "."
        raise SpotifyApiError(
            f"Spotify-API fehlgeschlagen (HTTP {response.status_code}){suffix}",
            status_code=response.status_code,
        )
    try:
        return response.json()
    except ValueError:
        return None


async def _get_token() -> str:
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token
    data = await _http_post_token()
    _token = data["access_token"]
    _token_expires = time.time() + float(data.get("expires_in", 3600))
    return _token


def _user_token() -> str | None:
    """Liefert den User-Token; None bedeutet Legacy-Suchpfad."""
    try:
        return oauth.access_token()
    except oauth.SpotifyNotAuthorized:
        return None


async def _user_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict | None:
    token = oauth.access_token()
    return await _http_request(method, path, token, params=params, body=body)


async def _device_id() -> str:
    """Wählt ein steuerbares Gerät ohne eine Geräte-ID dauerhaft zu cachen."""
    data = await _user_request("GET", "/me/player/devices")
    devices = (data or {}).get("devices") or []
    usable = [d for d in devices if d.get("id") and not d.get("is_restricted")]
    configured_name = cfg.SPOTIFY_DEVICE_NAME.strip().casefold()
    if configured_name:
        for device in usable:
            if device.get("name", "").strip().casefold() == configured_name:
                return device["id"]
        raise SpotifyApiError(
            f"Spotify-Gerät '{cfg.SPOTIFY_DEVICE_NAME}' nicht gefunden oder nicht steuerbar."
        )
    for device in usable:
        if device.get("is_active"):
            return device["id"]
    for device in usable:
        if device.get("type", "").casefold() == "computer":
            return device["id"]
    if usable:
        return usable[0]["id"]
    raise SpotifyApiError("Kein steuerbares Spotify-Gerät gefunden.")


async def _player_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict | None:
    """Sendet einen Player-Befehl an das frisch ermittelte Zielgerät."""
    device_id = await _device_id()
    player_params = {"device_id": device_id, **(params or {})}
    return await _user_request(method, path, params=player_params, body=body)


def _display_name(kind: str, item: dict) -> str:
    if kind == "track":
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
        return f"{item.get('name', '?')} — {artists}" if artists else item.get("name", "?")
    return item.get("name", "?")


async def search(query: str, typ: str | None = None) -> tuple[str, str] | None:
    """Beste Übereinstimmung → (URI, Anzeigename) oder None ohne Treffer.

    typ erzwingt einen Ergebnistyp (z.B. 'playlist'); ohne typ gilt die
    Präferenz track > album > playlist > artist.
    """
    kinds = (typ,) if typ in _TYPES else _TYPES
    token = _user_token() or await _get_token()
    data = await _http_get_search(
        {"q": query, "type": ",".join(kinds), "limit": 3, "market": "DE"}, token)
    for kind in kinds:
        items = (data.get(kind + "s") or {}).get("items") or []
        # Spotify liefert gelegentlich null-Einträge in Listen — überspringen.
        for item in items:
            if item and item.get("uri"):
                return item["uri"], _display_name(kind, item)
    return None


async def resume() -> None:
    """Setzt die Wiedergabe auf dem aktuell aktiven Gerät fort."""
    await _player_request("PUT", "/me/player/play")


async def pause() -> None:
    """Pausiert die Wiedergabe auf dem aktuell aktiven Gerät."""
    await _player_request("PUT", "/me/player/pause")


async def next_track() -> None:
    """Springt zum nächsten Titel."""
    await _player_request("POST", "/me/player/next")


async def previous_track() -> None:
    """Springt zum vorherigen Titel."""
    await _player_request("POST", "/me/player/previous")


async def set_volume(volume: int) -> None:
    """Setzt die Lautstärke des aktuell aktiven Geräts."""
    await _player_request("PUT", "/me/player/volume", params={"volume_percent": volume})


async def play_uri(uri: str) -> None:
    """Startet einen Track oder einen Spotify-Kontext (Album/Playlist/Künstler)."""
    parts = uri.split(":", 2)
    if len(parts) != 3 or parts[0] != "spotify":
        raise SpotifyApiError("Ungültige Spotify-URI.")
    body = {"uris": [uri]} if parts[1] == "track" else {"context_uri": uri}
    await _player_request("PUT", "/me/player/play", body=body)


async def current_track() -> dict | None:
    """Liest den aktuell laufenden Titel im User-Playback-Kontext."""
    data = await _user_request("GET", "/me/player", params={"additional_types": "track"})
    item = (data or {}).get("item") or {}
    if not item:
        return None
    artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
    return {
        "title": item.get("name", "?"),
        "artist": artists,
        "album": (item.get("album") or {}).get("name", ""),
        "playing": bool((data or {}).get("is_playing")),
    }
