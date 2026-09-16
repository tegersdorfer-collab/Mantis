"""Tests für die Spotify-Steuerung (tools/spotify/ + core/skills/spotify.py) — ohne echtes Spotify.

Der osascript-Aufruf und die Web-API-HTTP-Helfer werden gemockt; geprüft werden
Script-Erzeugung, Edge-Cases (gestoppter Player, fehlende Credentials) und die
Antwort-Formatierung des registrierten Tools.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.spotify import applescript as sp


def _patch_osa(calls, reply=""):
    """Ersetzt den osascript-Subprozess. reply: str oder Callable(script)->str."""
    async def fake(script: str) -> str:
        calls.append(script)
        return reply(script) if callable(reply) else reply
    sp._osascript = fake


# ── AppleScript-Treiber ───────────────────────────────────────────────────────

def test_playback_commands_build_correct_scripts():
    calls: list = []
    _patch_osa(calls)
    asyncio.run(sp.play())
    asyncio.run(sp.pause())
    asyncio.run(sp.next_track())
    asyncio.run(sp.previous_track())
    assert calls == [
        'tell application "Spotify" to play',
        'tell application "Spotify" to pause',
        'tell application "Spotify" to next track',
        'tell application "Spotify" to previous track',
    ]


def test_volume_is_clamped():
    calls: list = []
    _patch_osa(calls)
    asyncio.run(sp.set_volume(150))
    asyncio.run(sp.set_volume(-5))
    assert calls == [
        'tell application "Spotify" to set sound volume to 100',
        'tell application "Spotify" to set sound volume to 0',
    ]


def test_get_volume_parses_int():
    calls: list = []
    _patch_osa(calls, reply="63")
    assert asyncio.run(sp.get_volume()) == 63


def test_play_uri_escapes_quotes():
    calls: list = []
    _patch_osa(calls)
    asyncio.run(sp.play_uri('spotify:track:abc"def'))
    assert calls == ['tell application "Spotify" to play track "spotify:track:abcdef"']


def test_current_track_when_stopped_returns_none():
    """Live beobachtet: bei 'stopped' wirft 'current track' Fehler -1728 —
    der Treiber muss den State VORHER prüfen und None liefern."""
    calls: list = []
    _patch_osa(calls, reply="stopped")
    assert asyncio.run(sp.current_track()) is None
    assert len(calls) == 1  # kein zweiter Aufruf, der crashen würde


def test_current_track_playing():
    def reply(script):
        if "player state" in script:
            return "playing"
        return "Kids\nMGMT\nOracular Spectacular"
    calls: list = []
    _patch_osa(calls, reply=reply)
    t = asyncio.run(sp.current_track())
    assert t is not None
    assert (t.title, t.artist, t.album, t.state) == ("Kids", "MGMT", "Oracular Spectacular", "playing")

from settings import cfg
from tools.spotify import oauth
from tools.spotify import web_api


@pytest.fixture(autouse=True)
def isolate_spotify_oauth(monkeypatch, tmp_path):
    """Verhindert, dass Tests den echten lokalen Spotify-Token verwenden."""
    monkeypatch.setattr(oauth, "TOKEN_PATH", tmp_path / "spotify_token.json")


def _reset_webapi():
    web_api._token = None
    web_api._token_expires = 0.0


def _patch_http(search_json, token_calls=None):
    async def fake_token():
        if token_calls is not None:
            token_calls.append(1)
        return {"access_token": "tok123", "expires_in": 3600}

    async def fake_search(params, token):
        assert token == "tok123"
        fake_search.last_params = params
        return search_json
    web_api._http_post_token = fake_token
    web_api._http_get_search = fake_search
    return fake_search


# ── Web-API-Suche ─────────────────────────────────────────────────────────────

def test_credentials_missing_detection():
    old = (cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET)
    try:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = "", ""
        assert web_api.credentials_missing() is True
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = "id", "secret"
        assert web_api.credentials_missing() is False
    finally:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = old


def test_search_prefers_track_and_formats_name():
    _reset_webapi()
    _patch_http({
        "tracks": {"items": [{"uri": "spotify:track:t1", "name": "Kids",
                              "artists": [{"name": "MGMT"}]}]},
        "albums": {"items": [{"uri": "spotify:album:a1", "name": "Oracular"}]},
    })
    uri, name = asyncio.run(web_api.search("kids"))
    assert uri == "spotify:track:t1"
    assert name == "Kids — MGMT"


def test_search_type_hint_playlist():
    _reset_webapi()
    fake = _patch_http({
        "playlists": {"items": [{"uri": "spotify:playlist:p1", "name": "Focus Mix"}]},
    })
    uri, name = asyncio.run(web_api.search("focus", typ="playlist"))
    assert uri == "spotify:playlist:p1"
    assert name == "Focus Mix"
    assert fake.last_params["type"] == "playlist"


def test_search_no_results_returns_none():
    _reset_webapi()
    _patch_http({"tracks": {"items": []}, "albums": {"items": []},
                 "playlists": {"items": []}, "artists": {"items": []}})
    assert asyncio.run(web_api.search("qqqxyz")) is None


def test_token_is_cached():
    _reset_webapi()
    token_calls: list = []
    _patch_http({"tracks": {"items": [{"uri": "spotify:track:t1", "name": "A",
                                       "artists": [{"name": "B"}]}]}}, token_calls)
    asyncio.run(web_api.search("a"))
    asyncio.run(web_api.search("a"))
    assert len(token_calls) == 1  # zweiter Call nutzt den gecachten Token


# ── Registriertes Tool ────────────────────────────────────────────────────────

def test_skill_status_stopped():
    calls: list = []
    _patch_osa(calls, reply="stopped")
    from core.skills.spotify import _spotify
    assert asyncio.run(_spotify("status")) == "🔇 Gerade läuft nichts."


def test_skill_status_playing():
    def reply(script):
        return "playing" if "player state" in script else "Kids\nMGMT\nOracular"
    _patch_osa([], reply=reply)
    from core.skills.spotify import _spotify
    assert asyncio.run(_spotify("status")) == "🎵 Kids — MGMT · Oracular"


def test_skill_pause_and_volume():
    calls: list = []
    _patch_osa(calls)
    from core.skills.spotify import _spotify
    assert asyncio.run(_spotify("pause")) == "⏸️ Pausiert"
    assert asyncio.run(_spotify("volume", volume=40)) == "🔊 Lautstärke 40 %"
    assert asyncio.run(_spotify("volume")) == "❌ Bitte volume 0-100 angeben."


def test_skill_spiel_without_credentials_gives_setup_hint():
    from core.skills import spotify as skill
    old = (cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET)
    try:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = "", ""
        out = asyncio.run(skill._spotify("spiel", query="kids"))
        assert "developer.spotify.com" in out
    finally:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = old


def test_skill_spiel_plays_best_hit():
    _reset_webapi()
    calls: list = []
    _patch_osa(calls)
    _patch_http({"tracks": {"items": [{"uri": "spotify:track:t1", "name": "Kids",
                                       "artists": [{"name": "MGMT"}]}]}})
    from core.skills import spotify as skill
    old = (cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET)
    try:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = "id", "secret"
        out = asyncio.run(skill._spotify("spiel", query="kids"))
    finally:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = old
    assert out == "▶️ Kids — MGMT"
    assert calls[-1] == 'tell application "Spotify" to play track "spotify:track:t1"'


def test_skill_osascript_error_is_friendly():
    async def broken(script):
        raise sp.SpotifyError("kaputt")
    sp._osascript = broken
    from core.skills.spotify import _spotify
    out = asyncio.run(_spotify("play"))
    assert out.startswith("❌ Spotify nicht steuerbar")


def test_spotify_tool_is_registered():
    import core.skills  # noqa: F401 — löst Registrierung aus
    from core import tools as T
    assert "spotify" in T.REGISTRY
    assert T.REGISTRY["spotify"].category == "spotify"


# ── Spicetify-Bridge bevorzugt (mit Fallback) ─────────────────────────────────

from tools.spotify.bridge import BRIDGE, BridgeError


def _bridge_connected(monkey):
    """Markiert die Bridge als verbunden und mockt ihre Methoden über monkey-dict."""
    BRIDGE._ws = object()
    BRIDGE.search = monkey.get("search")
    BRIDGE.now_playing = monkey.get("now_playing")
    BRIDGE.play = monkey.get("play")


def _bridge_reset():
    BRIDGE._ws = None


def test_spiel_prefers_bridge():
    played = []

    async def fake_search(q, typ=None):
        return {"uri": "spotify:track:b1", "name": "Kids — MGMT"}

    async def fake_play(uri):
        played.append(uri)
    _bridge_connected({"search": fake_search, "play": fake_play})
    try:
        from core.skills.spotify import _spotify
        out = asyncio.run(_spotify("spiel", query="kids"))
    finally:
        _bridge_reset()
    assert out == "▶️ Kids — MGMT"
    assert played == ["spotify:track:b1"]


def test_status_prefers_bridge():
    async def fake_np():
        return {"title": "Kids", "artist": "MGMT", "album": "Oracular", "playing": True}
    _bridge_connected({"now_playing": fake_np})
    try:
        from core.skills.spotify import _spotify
        out = asyncio.run(_spotify("status"))
    finally:
        _bridge_reset()
    assert out == "🎵 Kids — MGMT · Oracular"


def test_spiel_falls_back_when_bridge_errors():
    async def bad_search(q, typ=None):
        raise BridgeError("kaputt")
    _bridge_connected({"search": bad_search})
    _reset_webapi()
    calls: list = []
    _patch_osa(calls)
    _patch_http({"tracks": {"items": [{"uri": "spotify:track:w1", "name": "W",
                                       "artists": [{"name": "X"}]}]}})
    old = (cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET)
    try:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = "id", "secret"
        from core.skills.spotify import _spotify
        out = asyncio.run(_spotify("spiel", query="w"))
    finally:
        cfg.SPOTIFY_CLIENT_ID, cfg.SPOTIFY_CLIENT_SECRET = old
        _bridge_reset()
    assert out == "▶️ W — X"        # Fallback-Web-API-Weg hat gegriffen


# ── Offizielle Web-API mit User-OAuth ─────────────────────────────────────────

def test_oauth_authorize_url_uses_loopback_and_playback_scopes():
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(oauth.build_authorize_url("client-1", "state-1")).query)
    assert query["client_id"] == ["client-1"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8084/callback"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-1"]
    assert set(query["scope"][0].split()) == {
        "user-modify-playback-state",
        "user-read-playback-state",
        "user-read-currently-playing",
    }


def test_oauth_refreshes_expired_token_and_keeps_refresh_token(tmp_path, monkeypatch):
    token_path = tmp_path / "spotify_token.json"
    monkeypatch.setattr(oauth, "TOKEN_PATH", token_path)
    oauth.save_token({
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": 0,
    })
    calls = []

    def fake_token_request(data):
        calls.append(data)
        return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_token_request", fake_token_request)
    assert oauth.access_token() == "new-access"
    assert calls == [{"grant_type": "refresh_token", "refresh_token": "old-refresh"}]
    assert oauth.load_token()["refresh_token"] == "old-refresh"


def test_web_api_search_prefers_user_token(monkeypatch):
    monkeypatch.setattr(oauth, "access_token", lambda: "user-token")

    async def fake_search(params, token):
        assert token == "user-token"
        assert params["type"] == "track,album,playlist,artist"
        return {"tracks": {"items": [{
            "uri": "spotify:track:user1", "name": "Kids",
            "artists": [{"name": "MGMT"}],
        }]}}

    monkeypatch.setattr(web_api, "_http_get_search", fake_search)
    uri, name = asyncio.run(web_api.search("kids"))
    assert (uri, name) == ("spotify:track:user1", "Kids — MGMT")


def test_web_api_starts_track_playback_with_user_token(monkeypatch):
    calls = []

    async def fake_request(method, path, token, *, params=None, body=None):
        calls.append((method, path, token, params, body))
        if path == "/me/player/devices":
            return {"devices": [{
                "id": "mac-1", "name": "Mantis Mac", "type": "Computer",
                "is_active": False, "is_restricted": False,
            }]}
        return None

    monkeypatch.setattr(oauth, "access_token", lambda: "user-token")
    monkeypatch.setattr(web_api, "_http_request", fake_request)
    asyncio.run(web_api.play_uri("spotify:track:abc"))
    assert calls == [
        ("GET", "/me/player/devices", "user-token", None, None),
        ("PUT", "/me/player/play", "user-token", {"device_id": "mac-1"},
         {"uris": ["spotify:track:abc"]}),
    ]


def test_web_api_starts_context_playback_with_context_uri(monkeypatch):
    calls = []

    async def fake_request(method, path, token, *, params=None, body=None):
        calls.append((method, path, token, params, body))
        if path == "/me/player/devices":
            return {"devices": [{
                "id": "mac-1", "name": "Mantis Mac", "type": "Computer",
                "is_active": False, "is_restricted": False,
            }]}
        return None

    monkeypatch.setattr(oauth, "access_token", lambda: "user-token")
    monkeypatch.setattr(web_api, "_http_request", fake_request)
    asyncio.run(web_api.play_uri("spotify:playlist:focus"))
    assert calls[-1][-1] == {"context_uri": "spotify:playlist:focus"}


def test_web_api_current_track_maps_playback_response(monkeypatch):
    async def fake_request(method, path, token, *, params=None, body=None):
        assert (method, path, token) == ("GET", "/me/player", "user-token")
        return {
            "is_playing": False,
            "item": {
                "name": "Kids",
                "artists": [{"name": "MGMT"}],
                "album": {"name": "Oracular Spectacular"},
            },
        }

    monkeypatch.setattr(oauth, "access_token", lambda: "user-token")
    monkeypatch.setattr(web_api, "_http_request", fake_request)
    assert asyncio.run(web_api.current_track()) == {
        "title": "Kids",
        "artist": "MGMT",
        "album": "Oracular Spectacular",
        "playing": False,
    }


def test_skill_prefers_official_api_for_pause(monkeypatch):
    calls = []

    async def fake_pause():
        calls.append("api")

    async def forbidden_applescript():
        raise AssertionError("AppleScript darf bei erfolgreicher API nicht laufen")

    monkeypatch.setattr(web_api, "pause", fake_pause)
    monkeypatch.setattr(sp, "pause", forbidden_applescript)
    from core.skills.spotify import _spotify
    assert asyncio.run(_spotify("pause")) == "⏸️ Pausiert"
    assert calls == ["api"]


def test_skill_spiel_prefers_official_api_when_oauth_exists(monkeypatch):
    played = []

    async def fake_search(query, typ=None):
        assert (query, typ) == ("kids", None)
        return "spotify:track:api1", "Kids — MGMT"

    async def fake_play(uri):
        played.append(uri)

    monkeypatch.setattr(oauth, "has_token", lambda: True)
    monkeypatch.setattr(web_api, "search", fake_search)
    monkeypatch.setattr(web_api, "play_uri", fake_play)
    monkeypatch.setattr(BRIDGE, "is_connected", lambda: False)
    from core.skills.spotify import _spotify
    assert asyncio.run(_spotify("spiel", query="kids")) == "▶️ Kids — MGMT"
    assert played == ["spotify:track:api1"]
