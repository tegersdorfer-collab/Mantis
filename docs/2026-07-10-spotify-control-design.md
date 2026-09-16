# Spotify-Steuerung — Design

**Datum:** 2026-07-10 · **Status:** vom User freigegeben („passt, bau es")

## Ziel

Mantis kann Spotify auf dem Mac bedienen: Play/Pause/Next/Previous, Lautstärke,
„Was läuft?" und „Spiel [Song/Album/Playlist/Künstler]". Steuerung läuft voll
lokal über AppleScript (`osascript`); nur die Suche für „Spiel [X]" nutzt die
Spotify-Web-API (zunächst Client-Credentials-Flow für die Suche; nach der
Premium-Umstellung User-OAuth für Suche und Playback).

**Verifiziert:** Der installierte Client ist mit Spicetify gepatcht — das ändert
nur die UI-Schicht (xpui), die AppleScript-Schnittstelle antwortet normal
(`player state` → „stopped", `sound volume` → 100, Bundle-ID `com.spotify.client`).

## Architektur

Exakt nach Flipper-Vorbild (Treiber in `tools/`, Registrierung in `core/skills/`):

### `tools/spotify/applescript.py`
Dünner async-Wrapper um `osascript -e` via `asyncio.create_subprocess_exec`:
- `play()`, `pause()`, `playpause()`, `next_track()`, `previous_track()`
- `set_volume(0–100)`, `get_volume()`
- `current_track()` → Titel, Künstler, Album, Player-Status
- `play_uri("spotify:…")` — startet Spotify automatisch, falls zu

**Edge-Case (live beobachtet):** Bei Player-Status „stopped" wirft `current track`
AppleScript-Fehler −1728. `current_track()` muss das abfangen und „nichts läuft"
zurückgeben statt zu crashen.

### `tools/spotify/web_api.py`
- User-Token via `tools/spotify/oauth.py` für Suche und Premium-Playback;
  Client-Credentials bleiben vorübergehend als Such-Fallback
- `search(query)` über Tracks/Alben/Playlists/Künstler → beste URI + Anzeigename
- `play_uri(uri)` startet einen Track oder Kontext (Album/Playlist/Künstler)
- `resume()`, `pause()`, `next_track()`, `previous_track()`, `set_volume()` und
  `current_track()` verwenden die Player-Endpunkte
- Player-Befehle ermitteln die Geräte-ID frisch; bevorzugt wird `SPOTIFY_DEVICE_NAME`,
  sonst ein aktives bzw. verfügbarer Computer. IDs werden nicht dauerhaft gecacht.

### `tools/spotify/oauth.py` und `scripts/spotify_auth.py`

- Server-seitiger Authorization-Code-Flow für den langlebigen lokalen Backend-
  Prozess
- Loopback-Callback `http://127.0.0.1:8084/callback`
- Access-/Refresh-Token in `data/spotify_token.json`, automatische Erneuerung

### `core/skills/spotify.py`
Ein `spotify`-Tool via `@T.register`:
- `action`-Enum: `play`, `pause`, `next`, `previous`, `volume`, `status`, `spiel`
- optionale Felder: `query` (für `spiel`), `volume` (0–100)
- Antworten im Emoji-Stil der anderen Tools (▶️ ⏸️ ⏭️ 🔊 🎵 …)

### `core/fast_commands.py` — Fast-Paths (eng, Design wie gehabt)
Fehlalarme schlimmer als Auslassungen. Play/Pause/Next/Prev greifen nur wenn:
- ein Musik-Kontextwort dabei ist (musik, spotify, lied, song, track), **oder**
- die Äußerung ein Ein-Wort-Befehl ist („pause", „skip", „next")

Nie bei Fragezeichen. „musik weiter" = Play, „nächstes lied" = Next.
Volume und „Spiel [X]" laufen bewusst über den Agenten (nicht kritisch,
brauchen Parameter-Verständnis).
Negativ-Beispiele, die NICHT triggern dürfen: „ich mach mal Pause",
„weiter gehts mit dem Projekt", „läuft gerade Musik?".

## Fehlerbehandlung

- Spotify nicht installiert / osascript-Fehler → ❌-Meldung im Flipper-Stil
- Suche ohne Treffer → ehrliche Antwort („nichts gefunden zu ‚…'")
- Web-API nicht erreichbar (offline) → Basisbefehle unberührt, Suche meldet es

## Tests (TDD)

- osascript-Subprozess gemockt (wie `tools/flipper`-Tests), httpx gemockt
- Fast-Path-Regeln: eigene Batterie inkl. aller Negativ-/Fehlalarm-Fälle
- Edge-Cases: stopped-Player, fehlende Credentials, kein Suchtreffer

## Setup (einmalig, User)

Auf developer.spotify.com eine Web-API-App anlegen, die Redirect URI
`http://127.0.0.1:8084/callback` eintragen, `SPOTIFY_CLIENT_ID` und
`SPOTIFY_CLIENT_SECRET` in `.env` setzen und `python3 scripts/spotify_auth.py`
einmalig ausführen. Ohne OAuth bleiben die Legacy-Fallbacks nutzbar.
