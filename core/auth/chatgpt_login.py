"""Einmaliges lokales Sign-in für die ChatGPT-Plan-Chatstufe.

Aufruf: ``python3.14 -m core.auth.chatgpt_login``
"""
from __future__ import annotations

import argparse
import http.server
import logging
import secrets
import socket
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

import config
from core.auth.chatgpt import (
    CALLBACK_PATH,
    REGISTRATION_CLIENT_ID,
    build_authorization_request,
    exchange_authorization_code,
    fetch_jwks,
    load_auth,
    register_token_secrets,
    require_direct_token_scope,
    save_auth,
    validate_callback,
    validate_id_token,
)
from core.error_redaction import redact_error_text

log = logging.getLogger(__name__)
LOGIN_TIMEOUT_S = 300


class _CallbackServer(http.server.HTTPServer):
    callback_result: dict[str, str] | None = None
    callback_error: str | None = None
    request_timeout_s = 10.0

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(min(10.0, max(0.001, float(self.request_timeout_s))))
        return request, address


def _callback_handler(expected_state: str, require_client_id: bool):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            try:
                self.server.callback_result = validate_callback(
                    parse_qs(parsed.query), expected_state, require_client_id=require_client_id,
                )
                body = b"Mantis ist angemeldet. Dieses Fenster kann geschlossen werden."
                status = 200
            except ValueError as exc:
                self.server.callback_error = str(exc)
                body = b"Anmeldung abgelehnt. Bitte im Terminal den Status pruefen."
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            # HTTPServer würde Query-String und damit Code/State ins Log schreiben.
            return

    return Handler


def _read_previous_auth(path: str | Path) -> dict | None:
    try:
        return load_auth(path)
    except ValueError:
        return None


def run_login(
    *,
    auth_file: str | Path | None = None,
    callback_port: int | None = None,
    timeout_s: float = LOGIN_TIMEOUT_S,
    http_client: httpx.Client | None = None,
    open_browser=webbrowser.open,
) -> None:
    """Führt Callback, Code-Tausch, OIDC-Prüfung und atomare Speicherung aus."""
    deadline = time.monotonic() + timeout_s
    auth_path = Path(auth_file or config.CHATGPT_AUTH_FILE).expanduser()
    port = int(callback_port if callback_port is not None else config.CHATGPT_AUTH_CALLBACK_PORT)
    previous = _read_previous_auth(auth_path)
    issued_client_id = str((previous or {}).get("client_id") or "")
    first_registration = not issued_client_id.startswith("oaiapp_")
    if first_registration:
        issued_client_id = REGISTRATION_CLIENT_ID
    host_id = str((previous or {}).get("ext_agent_host_id") or secrets.token_urlsafe(32))

    server = _CallbackServer(
        ("127.0.0.1", port),
        _callback_handler("", first_registration),
    )
    redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
    authorization = build_authorization_request(
        client_id=issued_client_id,
        redirect_uri=redirect_uri,
        ext_agent_host_id=host_id,
        first_registration=first_registration,
    )
    server.RequestHandlerClass = _callback_handler(authorization.state, first_registration)

    client_owned = http_client is None
    client = http_client or httpx.Client(timeout=20)
    server_closed = False

    def close_server() -> None:
        nonlocal server_closed
        if not server_closed:
            server_closed = True
            server.server_close()

    try:
        opened = open_browser(authorization.url)
        if not opened:
            print(f"Öffne diese Anmeldeseite im Browser:\n{authorization.url}")
        if time.monotonic() >= deadline:
            raise TimeoutError("ChatGPT-Login-Callback kam nicht innerhalb von fünf Minuten an")
        while server.callback_result is None and server.callback_error is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ChatGPT-Login-Callback kam nicht innerhalb von fünf Minuten an")
            server.timeout = min(1.0, remaining)
            server.request_timeout_s = min(10.0, remaining)
            try:
                server.handle_request()
            except socket.timeout:
                continue
            if server.callback_result is not None or server.callback_error is not None:
                close_server()
        if server.callback_error:
            raise ValueError(server.callback_error)
        callback = server.callback_result or {}
        client_id = callback.get("client_id") or issued_client_id
        if not first_registration and callback.get("client_id") and callback["client_id"] != issued_client_id:
            raise ValueError("OAuth-Callback lieferte eine unerwartete Client-ID")
        if first_registration and not client_id.startswith("oaiapp_"):
            raise ValueError("OAuth-Callback lieferte keine ausgestellte oaiapp-Client-ID")

        token_data = exchange_authorization_code(
            client,
            client_id=client_id,
            code=callback["code"],
            code_verifier=authorization.code_verifier,
            redirect_uri=redirect_uri,
            deadline=deadline,
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("ChatGPT-Login-Gesamtfrist ist abgelaufen")
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        id_token = token_data.get("id_token")
        scope = token_data.get("scope")
        if not all(isinstance(value, str) and value for value in (access_token, refresh_token, id_token, scope)):
            raise RuntimeError("ChatGPT-Tokenantwort enthält erforderliche Felder nicht")
        register_token_secrets(access_token, refresh_token, id_token)
        require_direct_token_scope(scope)
        validate_id_token(
            id_token,
            client_id=client_id,
            nonce=authorization.nonce,
            jwks=fetch_jwks(client, deadline=deadline),
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("ChatGPT-Login-Gesamtfrist ist abgelaufen")
        expires_in = token_data.get("expires_in", 3600)
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise RuntimeError("ChatGPT-Tokenantwort enthält eine ungültige Gültigkeitsdauer")
        save_auth(auth_path, {
            "client_id": client_id,
            "ext_agent_host_id": host_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
            "scope": scope,
        })
    finally:
        close_server()
        if client_owned:
            client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mantis mit einem ChatGPT-Abo anmelden")
    parser.add_argument("--port", type=int, default=None, help="Callback-Port (Default: 1456)")
    args = parser.parse_args(argv)
    try:
        run_login(callback_port=args.port)
    except Exception as exc:
        log.error("ChatGPT-Anmeldung fehlgeschlagen: %s", type(exc).__name__)
        print(f"Anmeldung fehlgeschlagen: {redact_error_text(exc)}")
        return 1
    print("ChatGPT-Anmeldung gespeichert. Mantis kann die Abo-Stufe beim nächsten Start nutzen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
