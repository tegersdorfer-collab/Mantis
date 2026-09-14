"""Dashboard authentication for HTTP, SSE and WebSocket connections."""
import hashlib
import hmac
import time

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
import config

COOKIE = 'mantis_session'
SESSION_SECONDS = 7 * 24 * 60 * 60
PUBLIC_PATHS = {'/', '/index.html', '/sw.js', '/manifest.json', '/favicon.ico'}
TRIGGER_PATHS = {'/api/trigger/ping', '/api/trigger/estop', '/api/trigger/tool'}


def origins():
    result = [x.strip().rstrip('/') for x in config.DASHBOARD_ALLOWED_ORIGINS.split(',') if x.strip()]
    if any('*' in x or x == 'null' for x in result):
        raise ValueError('DASHBOARD_ALLOWED_ORIGINS must contain explicit origins')
    return result


def token_ok(value):
    expected = config.DASHBOARD_TOKEN
    return bool(expected) and hmac.compare_digest(value.encode(), expected.encode())


def session_value():
    expiry = str(int(time.time()) + SESSION_SECONDS)
    signature = hmac.new(config.DASHBOARD_TOKEN.encode(), ('session:' + expiry).encode(), hashlib.sha256).hexdigest()
    return expiry + '.' + signature


def session_ok(value):
    try:
        expiry, signature = value.split('.')
        if int(expiry) <= time.time():
            return False
        expected = hmac.new(config.DASHBOARD_TOKEN.encode(), ('session:' + expiry).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (ValueError, AttributeError):
        return False


def _echo_subprotocol(send, subprotocol):
    """Trägt das validierte Subprotocol in die websocket.accept-Nachricht ein.

    Browser können bei WebSockets keinen Authorization-Header setzen, deshalb
    reist das Token als 'bearer.<token>'-Subprotocol mit. RFC 6455 verlangt,
    dass der Server genau eines der angebotenen Protokolle zurückspiegelt —
    und zwar nur das, das hier tatsächlich geprüft wurde, damit nie ein
    unvalidierter, clientkontrollierter Wert reflektiert wird.
    """
    async def wrapped(message):
        if message['type'] == 'websocket.accept' and not message.get('subprotocol'):
            message = {**message, 'subprotocol': subprotocol}
        return await send(message)
    return wrapped


class DashboardAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        path = scope['path']
        # Only known shell assets are anonymous. Future routes default to protected.
        if scope['type'] == 'http' and path in PUBLIC_PATHS and scope['method'] in ('GET', 'HEAD'):
            return await self.app(scope, receive, send)
        origin = connection.headers.get('origin')
        scheme = {'ws': 'http', 'wss': 'https'}.get(connection.url.scheme, connection.url.scheme)
        same_origin = f'{scheme}://{connection.url.netloc}'
        status, error = 401, 'Authentication required'
        if origin and origin != same_origin and origin not in origins():
            status, error = 403, 'Origin not allowed'
        elif scope['type'] == 'http' and path in TRIGGER_PATHS:
            return await self.app(scope, receive, send)  # Router validates TRIGGER_TOKEN.
        elif not config.DASHBOARD_TOKEN:
            status, error = 503, 'Set DASHBOARD_TOKEN on the server'
        else:
            auth = connection.headers.get('authorization', '')
            bearer = auth[7:] if auth.lower().startswith('bearer ') else ''
            valid = token_ok(bearer)
            accepted_subprotocol = None
            # Header schlaegt Subprotocol: wer per Header authentifiziert ist,
            # bekommt nichts zurueckgespiegelt (das angebotene Protokoll wurde
            # nie geprueft).
            if scope['type'] == 'websocket' and not valid:
                for offered in scope.get('subprotocols', []):
                    if offered.startswith('bearer.') and token_ok(offered[7:]):
                        valid, accepted_subprotocol = True, offered
                        break
            # Login must prove possession of the long-lived token, never a cookie.
            if not (path == '/auth/session' and scope.get('method') == 'POST') and not auth:
                valid = valid or session_ok(connection.cookies.get(COOKIE, ''))
            if valid:
                if accepted_subprotocol:
                    send = _echo_subprotocol(send, accepted_subprotocol)
                return await self.app(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        response = JSONResponse({'error': error}, status_code=status, headers={'Cache-Control': 'no-store'})
        await response(scope, receive, send)
