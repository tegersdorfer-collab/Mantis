import base64
import hashlib
import json
import secrets
import socket
import time
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core.auth.chatgpt import (
    DIRECT_TOKEN_SCOPE,
    DISCOVERY_URL,
    ISSUER,
    build_authorization_request,
    exchange_authorization_code,
    is_auth_configured,
    save_auth,
    validate_callback,
    validate_id_token,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _is_probable_prime(number: int) -> bool:
    if number < 2:
        return False
    for prime in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if number % prime == 0:
            return number == prime
    d = number - 1
    shift = 0
    while d % 2 == 0:
        shift += 1
        d //= 2
    for base in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(base, d, number)
        if x in (1, number - 1):
            continue
        for _ in range(shift - 1):
            x = pow(x, 2, number)
            if x == number - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _rsa_keypair(kid="test-key"):
    p = _random_prime(1024)
    q = _random_prime(1024)
    while p == q:
        q = _random_prime(1024)
    modulus = p * q
    while modulus.bit_length() < 2048:
        q = _random_prime(1024)
        modulus = p * q
    exponent = 65537
    private_exponent = pow(exponent, -1, (p - 1) * (q - 1))
    size = (modulus.bit_length() + 7) // 8
    jwk = {
        "kty": "RSA", "kid": kid,
        "n": _b64url(modulus.to_bytes(size, "big")),
        "e": _b64url(exponent.to_bytes(3, "big")),
    }
    return (modulus, exponent, private_exponent), jwk


@pytest.fixture(scope="module")
def rsa_keypair():
    return _rsa_keypair()


def _signed_id_token(claims: dict, keypair=None, *, kid="test-key") -> tuple[str, dict]:
    if keypair is None:
        keypair = _rsa_keypair(kid)
    (modulus, exponent, private_exponent), jwk = keypair
    size = (modulus.bit_length() + 7) // 8
    header = {"alg": "RS256", "kid": kid}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(
        signing_input.encode(),
    ).digest()
    size = (modulus.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(size, "big")
    return f"{signing_input}.{_b64url(signature)}", jwk


def test_authorization_request_uses_pkce_state_nonce_and_registration_fields():
    request = build_authorization_request(
        client_id="dynamic_agent_client",
        redirect_uri="http://127.0.0.1:1456/auth/callback",
        ext_agent_host_id="host-stable-test",
        first_registration=True,
    )
    params = parse_qs(urlparse(request.url).query)

    assert urlparse(request.url).scheme == "https"
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["http://127.0.0.1:1456/auth/callback"]
    assert params["resource"] == ["https://api.openai.com/v1"]
    assert set(params["scope"][0].split()) == {
        "openid", "profile", "email", "offline_access", "resource.invoke", DIRECT_TOKEN_SCOPE,
    }
    assert params["state"] == [request.state]
    assert params["nonce"] == [request.nonce]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [
        _b64url(hashlib.sha256(request.code_verifier.encode()).digest()),
    ]
    assert params["ext_agent_host_id"] == ["host-stable-test"]
    assert params["agent_name_hint"] == ["Mantis"]


def test_callback_rejects_wrong_state_and_requires_initial_client_id():
    code = secrets.token_urlsafe(24)
    with pytest.raises(ValueError, match="state"):
        validate_callback({"code": code, "state": "wrong"}, "expected")

    with pytest.raises(ValueError, match="Client-ID"):
        validate_callback({"code": code, "state": "expected"}, "expected", require_client_id=True)


def test_authorization_code_exchange_uses_issued_client_and_form_fields():
    seen = {}
    access_value = secrets.token_urlsafe(32)
    refresh_value = secrets.token_urlsafe(32)
    code = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"access_token": access_value, "refresh_token": refresh_value})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = exchange_authorization_code(
        client,
        client_id="oaiapp-issued-test",
        code=code,
        code_verifier=verifier,
        redirect_uri="http://127.0.0.1:1456/auth/callback",
    )

    assert response["access_token"] == access_value
    assert seen["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert seen["form"] == {
        "grant_type": "authorization_code",
        "client_id": "oaiapp-issued-test",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": "http://127.0.0.1:1456/auth/callback",
        "resource": "https://api.openai.com/v1",
    }


def test_login_requires_direct_token_scope_in_granted_scope():
    scope = "openid profile offline_access resource.invoke"
    with pytest.raises(ValueError, match="chatgpt.tokens.use.direct"):
        from core.auth.chatgpt import require_direct_token_scope
        require_direct_token_scope(scope)


def test_dynamic_registration_client_is_never_accepted_as_a_saved_login(tmp_path):
    access_value = secrets.token_urlsafe(32)
    refresh_value = secrets.token_urlsafe(32)
    path = tmp_path / "chatgpt_auth.json"
    save_auth(path, {
        "client_id": "dynamic_agent_client", "ext_agent_host_id": "host-test",
        "access_token": access_value, "refresh_token": refresh_value,
        "expires_at": time.time() + 3600, "scope": f"openid {DIRECT_TOKEN_SCOPE}",
    })

    assert not is_auth_configured(path)


def test_auth_file_is_written_atomically_with_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "private" / "chatgpt_auth.json"
    calls = []
    import core.auth.chatgpt as auth
    original_replace = auth.os.replace
    monkeypatch.setattr(auth.os, "replace", lambda source, target: (calls.append((source, target)), original_replace(source, target))[1])

    access_value = secrets.token_urlsafe(32)
    refresh_value = secrets.token_urlsafe(32)
    save_auth(path, {
        "client_id": "oaiapp-test",
        "ext_agent_host_id": "host-test",
        "access_token": access_value,
        "refresh_token": refresh_value,
        "expires_at": time.time() + 3600,
        "scope": f"openid {DIRECT_TOKEN_SCOPE}",
    })

    assert path.exists()
    assert len(calls) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text())["client_id"] == "oaiapp-test"
    assert list(path.parent.iterdir()) == [path]


def test_id_token_verifies_rs256_issuer_audience_expiration_and_nonce(rsa_keypair):
    now = int(time.time())
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "oaiapp-issued-test",
        "exp": now + 300,
        "nonce": "login-nonce",
    }
    token, jwk = _signed_id_token(claims, rsa_keypair)

    result = validate_id_token(token, client_id="oaiapp-issued-test", nonce="login-nonce", jwks={"keys": [jwk]}, now=now)

    assert result["nonce"] == "login-nonce"
    header, payload, signature = token.split(".")
    signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    tampered_signature = _b64url(signature_bytes[:-1] + bytes([signature_bytes[-1] ^ 1]))
    invalid_signature = f"{header}.{payload}.{tampered_signature}"
    with pytest.raises(ValueError, match="Signatur"):
        validate_id_token(invalid_signature, client_id="oaiapp-issued-test", nonce="login-nonce", jwks={"keys": [jwk]}, now=now)

    for changed in (
        {**claims, "iss": "https://wrong.example"},
        {**claims, "aud": "another-client"},
        {**claims, "exp": now - 1},
        {**claims, "nonce": "different"},
    ):
        bad_token, bad_jwk = _signed_id_token(changed, rsa_keypair)
        with pytest.raises(ValueError):
            validate_id_token(bad_token, client_id="oaiapp-issued-test", nonce="login-nonce", jwks={"keys": [bad_jwk]}, now=now)

    wrong_keypair = _rsa_keypair()
    wrong_key_token, _ = _signed_id_token(claims, wrong_keypair)
    with pytest.raises(ValueError, match="Signatur"):
        validate_id_token(wrong_key_token, client_id="oaiapp-issued-test", nonce="login-nonce", jwks={"keys": [jwk]}, now=now)

    wrong_kid_token, _ = _signed_id_token(claims, rsa_keypair, kid="wrong-key")
    with pytest.raises(ValueError, match="Signatur"):
        validate_id_token(wrong_kid_token, client_id="oaiapp-issued-test", nonce="login-nonce", jwks={"keys": [jwk]}, now=now)


class _FakeCallbackServer:
    instances = []

    def __init__(self, address, _handler):
        self.server_port = address[1]
        self.callback_result = None
        self.callback_error = None
        self.closed = False
        self.instances.append(self)

    def handle_request(self):
        self.callback_result = {"code": "login-code", "client_id": "oaiapp_issued_test"}

    def server_close(self):
        self.closed = True


def test_login_closes_callback_server_before_token_exchange(monkeypatch, tmp_path):
    import core.auth.chatgpt_login as login

    _FakeCallbackServer.instances.clear()
    monkeypatch.setattr(login, "_CallbackServer", _FakeCallbackServer)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            assert _FakeCallbackServer.instances[0].closed
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise AssertionError(f"Unerwarteter HTTP-Aufruf: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        login.run_login(
            auth_file=tmp_path / "chatgpt_auth.json", callback_port=1456,
            timeout_s=2, http_client=client, open_browser=lambda _url: True,
        )
    client.close()

    assert _FakeCallbackServer.instances[0].closed


@pytest.mark.parametrize("invalid_claim", ["nonce", "issuer", "audience", "expiry", "signature"])
def test_login_does_not_save_tokens_when_id_token_validation_fails(
    invalid_claim, monkeypatch, tmp_path, rsa_keypair,
):
    import core.auth.chatgpt_login as login

    _FakeCallbackServer.instances.clear()
    monkeypatch.setattr(login, "_CallbackServer", _FakeCallbackServer)
    state = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            claims = {
                "iss": ISSUER,
                "aud": "oaiapp_issued_test",
                "exp": int(time.time()) + 300,
                "nonce": state["nonce"],
            }
            if invalid_claim == "nonce":
                claims["nonce"] = "wrong-nonce"
            elif invalid_claim == "issuer":
                claims["iss"] = "https://wrong.example"
            elif invalid_claim == "audience":
                claims["aud"] = "another-client"
            elif invalid_claim == "expiry":
                claims["exp"] = int(time.time()) - 1
            id_token, jwk = _signed_id_token(claims, rsa_keypair)
            if invalid_claim == "signature":
                header, payload, signature = id_token.split(".")
                signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
                changed_signature = _b64url(signature_bytes[:-1] + bytes([signature_bytes[-1] ^ 1]))
                id_token = f"{header}.{payload}.{changed_signature}"
            state["jwk"] = jwk
            return httpx.Response(200, json={
                "access_token": "access-test",
                "refresh_token": "refresh-test",
                "id_token": id_token,
                "scope": f"openid {DIRECT_TOKEN_SCOPE}",
                "expires_in": 3600,
            })
        if request.url == httpx.URL(DISCOVERY_URL):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/keys"})
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [state["jwk"]]})
        raise AssertionError(f"Unerwarteter HTTP-Aufruf: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def open_browser(url):
        state["nonce"] = parse_qs(urlparse(url).query)["nonce"][0]
        return True

    auth_file = tmp_path / "chatgpt_auth.json"
    try:
        with pytest.raises(ValueError):
            login.run_login(
                auth_file=auth_file, callback_port=1456, timeout_s=2,
                http_client=client, open_browser=open_browser,
            )
        assert not auth_file.exists()
    finally:
        client.close()


def test_callback_socket_without_request_respects_overall_login_deadline(
    monkeypatch, tmp_path,
):
    import core.auth.chatgpt_login as login

    servers = []

    class SocketPairListener:
        def __init__(self, accepted):
            self.accepted = accepted

        def accept(self):
            return self.accepted, ("socketpair", 0)

    class SocketPairServer(login._CallbackServer):
        def __init__(self, address, _handler):
            self.server_port = address[1]
            self.callback_result = None
            self.callback_error = None
            self.timeout = None
            self.request_timeout_s = 10
            self.closed = False
            self.accepted_socket, self.peer_socket = socket.socketpair()
            self.socket = SocketPairListener(self.accepted_socket)
            servers.append(self)

        def handle_request(self):
            request, _address = self.get_request()
            try:
                request.recv(1)
            finally:
                request.close()

        def server_close(self):
            self.closed = True
            self.peer_socket.close()

    monkeypatch.setattr(login, "_CallbackServer", SocketPairServer)
    outcome = {}

    def do_login():
        try:
            login.run_login(
                auth_file=tmp_path / "chatgpt_auth.json", callback_port=1456,
                timeout_s=0.15, open_browser=lambda _url: True,
            )
        except BaseException as exc:
            outcome["error"] = exc

    started = time.monotonic()
    worker = Thread(target=do_login, daemon=True)
    worker.start()
    worker.join(0.35)
    if worker.is_alive():
        try:
            servers[0].peer_socket.sendall(b"release")
        except OSError:
            pass
        worker.join(1)

    assert not worker.is_alive(), "Login hing trotz abgelaufener Gesamtfrist am Callback-Request"
    assert isinstance(outcome.get("error"), TimeoutError)
    assert time.monotonic() - started < 0.25


def test_callback_socket_slow_trickle_cannot_extend_overall_login_deadline(
    monkeypatch, tmp_path,
):
    import core.auth.chatgpt_login as login

    servers = []

    class SocketPairListener:
        def __init__(self, accepted):
            self.accepted = accepted

        def accept(self):
            return self.accepted, ("socketpair", 0)

    class SocketPairServer(login._CallbackServer):
        def __init__(self, address, handler):
            self.server_port = address[1]
            self.callback_result = None
            self.callback_error = None
            self.timeout = None
            self.request_timeout_s = 10
            self.deadline = None
            self.accepted_socket, self.peer_socket = socket.socketpair()
            self.socket = SocketPairListener(self.accepted_socket)
            self.RequestHandlerClass = handler
            servers.append(self)

        def handle_request(self):
            request, client_address = self.get_request()
            try:
                self.finish_request(request, client_address)
            finally:
                self.shutdown_request(request)

        def server_close(self):
            self.peer_socket.close()

    monkeypatch.setattr(login, "_CallbackServer", SocketPairServer)
    outcome = {}
    sender_stop = Event()

    def do_login():
        try:
            login.run_login(
                auth_file=tmp_path / "chatgpt_auth.json", callback_port=1456,
                timeout_s=0.15, open_browser=lambda _url: True,
            )
        except BaseException as exc:
            outcome["error"] = exc

    def trickle_request_line():
        while not sender_stop.wait(0.04):
            try:
                servers[0].peer_socket.sendall(b"x")
            except OSError:
                return

    started = time.monotonic()
    worker = Thread(target=do_login, daemon=True)
    worker.start()
    sender = Thread(target=trickle_request_line, daemon=True)
    sender.start()
    worker.join(0.25)
    finished_by_deadline = not worker.is_alive()
    sender_stop.set()
    try:
        servers[0].peer_socket.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    sender.join(0.2)
    if worker.is_alive():
        worker.join(1)

    assert finished_by_deadline, "Langsam eintreffende Bytes haben die Login-Gesamtfrist verlängert"
    assert isinstance(outcome.get("error"), TimeoutError)
    assert time.monotonic() - started < 0.25


def test_jwks_discovery_is_standard_oidc_and_uses_no_client_secret():
    request = build_authorization_request(
        client_id="oaiapp-issued-test",
        redirect_uri="http://127.0.0.1:1456/auth/callback",
        ext_agent_host_id="host-test",
        first_registration=False,
    )
    params = parse_qs(urlparse(request.url).query)
    assert params["client_id"] == ["oaiapp-issued-test"]
    assert "ext_agent_host_id" not in params
    assert "client_secret" not in params



def test_login_host_id_uses_urn_uuid_format():
    import inspect
    from core.auth import chatgpt_login

    source = inspect.getsource(chatgpt_login)
    assert 'f"urn:uuid:{uuid.uuid4()}"' in source
    assert 'startswith("urn:uuid:")' in source
