#!/usr/bin/env python3
"""
Einmaliger OAuth-Login für Mantis → COROS-MCP.

Ausführen:
  cd /Users/timoegersdorfer/Mantis
  python3 scripts/coros_auth.py

Registriert Mantis per Dynamic Client Registration bei COROS, öffnet den Login
im Browser und speichert data/coros_token.json. Danach hält der Refresh-Token
die Verbindung; das Skript muss nur bei Entzug oder Ablauf erneut laufen.
"""
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

import config
from domains.coros import oauth

PORT = 8083


def main():
    issuer = config.COROS_MCP_URL.rsplit("/mcp", 1)[0]
    print(f"🔎 Metadaten von {issuer} …")
    meta = oauth.discover(issuer)

    tok = oauth.load_token()
    client_id = tok["client_id"] if tok and tok.get("client_id") else oauth.register_client(meta)
    print(f"🪪 Client-ID: {client_id}")

    verifier, challenge = oauth.pkce_pair()
    state = secrets.token_urlsafe(16)
    url = oauth.build_authorize_url(meta, client_id, challenge, state)

    print("\n🌐 Öffne diesen Link und melde dich mit deinem COROS-Account an:\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in params.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = ("<h2>✅ COROS verbunden.</h2><p>Fenster kann zu.</p>" if ok
                    else "<h2>❌ Fehlgeschlagen.</h2><p>Siehe Terminal.</p>")
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *_):
            pass

    print(f"⏳ Warte auf den Callback auf Port {PORT} …")
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as srv:
        srv.handle_request()

    if result.get("state") != state:
        print(f"❌ State stimmt nicht — Abbruch. Antwort: {result}")
        sys.exit(1)
    if "code" not in result:
        print(f"❌ Kein Code zurückgekommen: {result}")
        sys.exit(1)

    tok = oauth.exchange_code(meta, client_id, result["code"], verifier)
    oauth.save_token(tok)
    print(f"✅ Token gespeichert in {oauth.TOKEN_PATH}")
    print("   Nächster Schritt: python3 scripts/coros_probe.py")


if __name__ == "__main__":
    main()
