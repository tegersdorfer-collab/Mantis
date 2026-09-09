from types import SimpleNamespace

import pytest
from fastapi import APIRouter, WebSocket
from fastapi.testclient import TestClient

import config
from web.api import create_app
from web import routers

TOKEN = 'a' * 48

@pytest.fixture
def client(monkeypatch):
    router = APIRouter()
    @router.get('/api/private')
    def private():
        return {'private': True}
    @router.post('/api/upload')
    async def upload():
        return {'ok': True}
    @router.websocket('/ws/test')
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_text('authenticated')
        await ws.close()
    @router.get('/api/stream')
    async def stream():
        from starlette.responses import StreamingResponse
        return StreamingResponse(iter(['data: authenticated\n\n']), media_type='text/event-stream')
    monkeypatch.setattr(routers, 'ROUTER_MODULES', [SimpleNamespace(build_router=lambda _: router)])
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', TOKEN, raising=False)
    monkeypatch.setattr(config, 'DASHBOARD_ALLOWED_ORIGINS', 'http://localhost:1420', raising=False)
    return TestClient(create_app())

def test_private_requires_auth(client):
    assert client.get('/api/private').status_code == 401
    assert client.get('/api/private?token=' + TOKEN).status_code == 401
    assert client.get('/health').status_code == 401
    assert client.post('/api/upload', files={'image': ('x.jpg', b'x')}).status_code == 401

def test_bearer_and_invalid_token(client):
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 200
    assert client.get('/api/private', headers={'Authorization': 'Bearer wrong'}).status_code == 401

def test_fail_closed_without_token(client, monkeypatch):
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', '')
    assert client.get('/api/private').status_code == 503

def test_login_cookie_and_origin_guard(client):
    response = client.post('/auth/session', headers={'Authorization': 'Bearer ' + TOKEN})
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert TOKEN not in response.headers['set-cookie']
    assert client.get('/api/private').status_code == 200
    assert client.post('/api/upload', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/private', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/auth/session').status_code == 401
    assert client.delete('/auth/session').status_code == 200
    assert client.get('/api/private').status_code == 401

def test_cors_explicit(client):
    headers = {'Origin': 'http://localhost:1420', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'}
    response = client.options('/api/upload', headers=headers)
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == headers['Origin']
    headers['Origin'] = 'https://evil.example'
    assert client.options('/api/upload', headers=headers).status_code == 400

@pytest.mark.parametrize('host', ['0.0.0.0', '::', '192.168.1.2', '8.8.8.8'])
def test_reject_exposed_bind(host):
    from main import validate_dashboard_host
    with pytest.raises(ValueError):
        validate_dashboard_host(host)

@pytest.mark.parametrize('host', ['127.0.0.1', '::1', '100.100.1.2', 'fd7a:115c:a1e0::1'])
def test_allow_private_bind(host):
    from main import validate_dashboard_host
    assert validate_dashboard_host(host) == host


def test_cookie_sse_upload_and_rotation(client, monkeypatch):
    client.post('/auth/session', headers={'Authorization': 'Bearer ' + TOKEN})
    assert client.get('/api/stream').text == 'data: authenticated\n\n'
    assert client.post('/api/upload', files={'image': ('x.jpg', b'x')}).status_code == 200
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', 'new-token')
    assert client.get('/api/private').status_code == 401


def test_expired_session(client, monkeypatch):
    import web.auth as auth
    client.post('/auth/session', headers={'Authorization': 'Bearer ' + TOKEN})
    now = auth.time.time()
    monkeypatch.setattr(auth.time, 'time', lambda: now + auth.SESSION_SECONDS + 1)
    assert client.get('/api/private').status_code == 401


def test_websocket_auth(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/test'):
            pass
    with client.websocket_connect('/ws/test', subprotocols=['mantis', 'bearer.' + TOKEN]) as ws:
        assert ws.accepted_subprotocol == 'bearer.' + TOKEN
        assert ws.receive_text() == 'authenticated'
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/test', subprotocols=['bearer.' + TOKEN], headers={'origin': 'https://evil.example'}):
            pass


def test_websocket_selects_only_validated_bearer_protocol(client):
    with client.websocket_connect('/ws/test', subprotocols=['bearer.wrong', 'bearer.' + TOKEN]) as ws:
        assert ws.accepted_subprotocol == 'bearer.' + TOKEN
        assert ws.receive_text() == 'authenticated'


def test_websocket_header_auth_does_not_echo_invalid_protocol(client):
    with client.websocket_connect('/ws/test', subprotocols=['bearer.wrong'], headers={'Authorization': 'Bearer ' + TOKEN}) as ws:
        assert ws.accepted_subprotocol is None
        assert ws.receive_text() == 'authenticated'


def test_websocket_cookie_auth_needs_no_protocol(client):
    client.post('/auth/session', headers={'Authorization': 'Bearer ' + TOKEN})
    with client.websocket_connect('/ws/test') as ws:
        assert ws.accepted_subprotocol is None
        assert ws.receive_text() == 'authenticated'
