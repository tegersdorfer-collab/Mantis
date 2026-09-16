#!/usr/bin/env python3
"""Einmaliger Spotify-OAuth-Login für Mantis.

Ausführen:
  cd /Users/timoegersdorfer/Mantis
  python3 scripts/spotify_auth.py

Der Login öffnet den Spotify-Browserdialog und wartet auf den lokalen Callback
unter http://127.0.0.1:8084/callback. Danach liegt der User-Token in
data/spotify_token.json; Mantis erneuert das Access-Token automatisch.
"""
from __future__ import annotations

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

from tools.spotify import oauth


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Nimmt genau den OAuth-Callback entgegen und ignoriert Browser-Nebenrequests."""

    result: dict[str, str] = {}
    expected_state = ""

    def do_GET(self):  # noqa: N802 - Name ist vom HTTP-Handler vorgegeben
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            params = urllib.parse.parse_qs(parsed.query)
            flat = {key: values[0] for key, values in params.items() if values}
            if "code" in flat or "error" in flat:
                self.result.update(flat)
                ok = "code" in flat and flat.get("state") == self.expected_state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = (
                    "<h2>✅ Spotify verbunden.</h2><p>Fenster kann geschlossen werden.</p>"
                    if ok else
                    "<h2>❌ Spotify-Login fehlgeschlagen.</h2><p>Siehe Terminal.</p>"
                )
                self.wfile.write(body.encode("utf-8"))
                return
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass


def main() -> None:
    if oauth.credentials_missing():
        print("❌ SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET fehlen in der .env.")
        print("   Zuerst eine Web-API-App auf https://developer.spotify.com anlegen.")
        sys.exit(1)

    state = secrets.token_urlsafe(24)
    url = oauth.build_authorize_url(state=state)
    _CallbackHandler.result = {}
    _CallbackHandler.expected_state = state

    print("🌐 Öffne Spotify zur Autorisierung …")
    print(f"   Falls sich kein Browser öffnet: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("⏳ Warte auf den OAuth-Callback auf http://127.0.0.1:8084/callback …")
    server = http.server.HTTPServer(("127.0.0.1", 8084), _CallbackHandler)
    while not ("code" in _CallbackHandler.result or "error" in _CallbackHandler.result):
        server.handle_request()
    server.server_close()

    result = _CallbackHandler.result
    if "error" in result:
        print(f"❌ Autorisierung abgelehnt: {result['error']}")
        sys.exit(1)
    if result.get("state") != state:
        print("❌ OAuth-State stimmt nicht — Abbruch.")
        sys.exit(1)
    if "code" not in result:
        print("❌ Kein Autorisierungscode erhalten.")
        sys.exit(1)

    try:
        token = oauth.exchange_code(result["code"])
        oauth.save_token(token)
    except oauth.SpotifyOAuthError as exc:
        print(f"❌ Spotify-Token konnte nicht gespeichert werden: {exc}")
        sys.exit(1)
    print(f"✅ Spotify-Token gespeichert: {oauth.TOKEN_PATH}")
    print("   Mantis kann Spotify jetzt direkt über die Web-API steuern.")


if __name__ == "__main__":
    main()
