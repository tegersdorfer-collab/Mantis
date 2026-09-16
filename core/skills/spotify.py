"""Spotify-Steuerung — bevorzugt über die offizielle Spotify-Web-API.

Registriert sich via @T.register beim Import (durch core/skills/__init__.py).
Mit Spotify-User-OAuth funktionieren Playback, Status und Suche direkt über die
Web-API. AppleScript und die Spicetify-Bridge bleiben als Rückfallebenen.
"""

import logging

from core import tools as T
from tools.spotify import applescript as sp
from tools.spotify import oauth
from tools.spotify import web_api
from tools.spotify.bridge import BRIDGE, BridgeError

log = logging.getLogger("core.skills")

_SETUP_HINT = (
    "🎧 Spotify ist noch nicht autorisiert: Spotify-App auf "
    "developer.spotify.com anlegen, SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET "
    "in die .env eintragen und einmalig `python3 scripts/spotify_auth.py` ausführen."
)

_API_ERRORS = (oauth.SpotifyOAuthError, web_api.SpotifyApiError)


@T.register(
    "spotify",
    "Steuert Spotify auf dem Mac: play/pause/next/previous, Lautstärke (volume 0-100), "
    "status = was läuft gerade, spiel = sucht Song/Album/Playlist/Künstler und spielt "
    "das beste Ergebnis ab. Nutze dies wenn Timo Musik hören, pausieren, wechseln, "
    "lauter/leiser stellen oder wissen will was gerade läuft.",
    {
        "action": {
            "type": "string",
            "enum": ["play", "pause", "next", "previous", "volume", "status", "spiel"],
            "description": "play/pause/next/previous = Playback, volume = Lautstärke setzen, "
                           "status = aktueller Track, spiel = suchen und abspielen",
        },
        "query": {"type": "string",
                  "description": "nur für spiel: Song/Album/Playlist/Künstler-Name"},
        "volume": {"type": "integer", "description": "nur für volume: Ziel-Lautstärke 0-100"},
        "typ": {
            "type": "string",
            "enum": ["track", "album", "playlist", "artist"],
            "description": "nur für spiel: optionaler Typ-Hinweis "
                           "(z.B. playlist wenn Timo 'die Playlist X' sagt)",
        },
    },
    ["action"],
    "spotify",
)
async def _spotify(action: str, query: str = "", volume: int = -1, typ: str = ""):
    a = (action or "").strip().lower()
    try:
        if a == "play":
            try:
                await web_api.resume()
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Play fehlgeschlagen, Fallback AppleScript: %s", e)
                await sp.play()
            return "▶️ Musik läuft"
        if a == "pause":
            try:
                await web_api.pause()
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Pause fehlgeschlagen, Fallback AppleScript: %s", e)
                await sp.pause()
            return "⏸️ Pausiert"
        if a == "next":
            try:
                await web_api.next_track()
                np = None
                try:
                    np = await web_api.current_track()
                except _API_ERRORS:
                    pass
                return (f"⏭️ {np['title']} — {np['artist']}" if np else "⏭️ Nächster Track")
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Next fehlgeschlagen, Fallback AppleScript: %s", e)
                await sp.next_track()
                t = await sp.current_track()
                return f"⏭️ {t.title} — {t.artist}" if t else "⏭️ Nächster Track"
        if a == "previous":
            try:
                await web_api.previous_track()
                np = None
                try:
                    np = await web_api.current_track()
                except _API_ERRORS:
                    pass
                return (f"⏮️ {np['title']} — {np['artist']}" if np else "⏮️ Vorheriger Track")
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Previous fehlgeschlagen, Fallback AppleScript: %s", e)
                await sp.previous_track()
                t = await sp.current_track()
                return f"⏮️ {t.title} — {t.artist}" if t else "⏮️ Vorheriger Track"
        if a == "volume":
            if volume is None or volume < 0:
                return "❌ Bitte volume 0-100 angeben."
            v = max(0, min(100, int(volume)))
            try:
                await web_api.set_volume(v)
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Lautstärke fehlgeschlagen, Fallback AppleScript: %s", e)
                await sp.set_volume(v)
            return f"🔊 Lautstärke {v} %"
        if a == "status":
            # Bevorzugt strukturiert über die offizielle User-API.
            try:
                np = await web_api.current_track()
                if np is None:
                    return "🔇 Gerade läuft nichts."
                suffix = "" if np.get("playing", True) else " (pausiert)"
                return (f"🎵 {np['title']} — {np.get('artist', '')} · "
                        f"{np.get('album', '')}{suffix}")
            except _API_ERRORS as e:
                log.info("Spotify-Web-API-Status fehlgeschlagen, Legacy-Fallback: %s", e)
            # Übergangspfad für Installationen ohne OAuth-Token.
            if BRIDGE.is_connected():
                try:
                    np = await BRIDGE.now_playing()
                    if np is None:
                        return "🔇 Gerade läuft nichts."
                    if np.get("title"):
                        suffix = "" if np.get("playing", True) else " (pausiert)"
                        return (f"🎵 {np['title']} — {np.get('artist', '')} · "
                                f"{np.get('album', '')}{suffix}")
                except BridgeError as e:
                    log.info("Bridge-Status fehlgeschlagen, Fallback AppleScript: %s", e)
            t = await sp.current_track()
            if t is None:
                return "🔇 Gerade läuft nichts."
            suffix = " (pausiert)" if t.state == "paused" else ""
            return f"🎵 {t.title} — {t.artist} · {t.album}{suffix}"
        if a == "spiel":
            return await _spiel(query, typ)
        return (f"❌ Unbekannte Spotify-Aktion '{action}'. Möglich: play, pause, next, "
                f"previous, volume, status, spiel.")
    except sp.SpotifyError as e:
        log.warning("spotify fehlgeschlagen: %s", e)
        return f"❌ Spotify nicht steuerbar: {e}. Ist Spotify installiert und gestartet?"


async def _spiel(query: str, typ: str = "") -> str:
    if not (query or "").strip():
        return "❌ Was soll ich spielen? Bitte query angeben."
    # Bevorzugt: offizielle Web-API im Namen des Premium-Nutzers.
    if oauth.has_token():
        try:
            hit = await web_api.search(query.strip(), typ=(typ or None))
            if hit is None:
                return f"🤷 Nichts gefunden zu ‚{query}'."
            uri, name = hit
            await web_api.play_uri(uri)
            return f"▶️ {name}"
        except (oauth.SpotifyOAuthError, web_api.SpotifySearchError) as e:
            log.info("Spotify-Web-API-Suche/Playback fehlgeschlagen, Legacy-Fallback: %s", e)

    # Übergangspfad: Spicetify-Bridge ohne OAuth.
    if BRIDGE.is_connected():
        try:
            hit = await BRIDGE.search(query.strip(), typ=(typ or None))
            if hit is None:
                return f"🤷 Nichts gefunden zu ‚{query}'."
            await BRIDGE.play(hit["uri"])
            return f"▶️ {hit['name']}"
        except BridgeError as e:
            log.info("Bridge-Suche fehlgeschlagen, Fallback Web-API: %s", e)
    if web_api.credentials_missing():
        return _SETUP_HINT
    try:
        hit = await web_api.search(query.strip(), typ=(typ or None))
    except web_api.SpotifySearchError as e:
        log.warning("spotify-suche fehlgeschlagen: %s", e)
        return f"❌ Spotify-Suche gerade nicht möglich: {e}"
    if hit is None:
        return f"🤷 Nichts gefunden zu ‚{query}'."
    uri, name = hit
    await sp.play_uri(uri)
    return f"▶️ {name}"
