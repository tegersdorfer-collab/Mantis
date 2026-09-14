"""Local dashboard clients must follow configured host/port and authenticate."""
import httpx
import config
from web.client_auth import dashboard_url, dashboard_headers


def test_dashboard_connection_settings(monkeypatch):
    monkeypatch.setattr(config, 'DASHBOARD_HOST', 'fd7a:115c:a1e0::1')
    monkeypatch.setattr(config, 'DASHBOARD_PORT', 7780)
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', 'test-only-secret')
    assert dashboard_url() == 'http://[fd7a:115c:a1e0::1]:7780'
    assert dashboard_headers() == {'Authorization': 'Bearer test-only-secret'}


def test_mcp_authenticates_task_request(monkeypatch):
    from web.mcp_server import _handle_mcp_call
    monkeypatch.setattr(config, 'DASHBOARD_HOST', '100.64.1.2')
    monkeypatch.setattr(config, 'DASHBOARD_PORT', 7780)
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', 'test-only-secret')
    def get(url, **kwargs):
        assert url == 'http://100.64.1.2:7780/api/tasks'
        assert kwargs['headers'] == {'Authorization': 'Bearer test-only-secret'}
        return httpx.Response(200, json=[{'title': 'Example', 'priority': 1}])
    monkeypatch.setattr(httpx, 'get', get)
    assert 'Example' in _handle_mcp_call('mantis_get_tasks', {})


def test_watchdog_authenticates_status_check(monkeypatch):
    from scripts import restart_watchdog
    monkeypatch.setattr(config, 'DASHBOARD_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'DASHBOARD_PORT', 7780)
    monkeypatch.setattr(config, 'DASHBOARD_TOKEN', 'test-only-secret')
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def urlopen(request, **kwargs):
        assert request.full_url == 'http://127.0.0.1:7780/api/status'
        assert request.get_header('Authorization') == 'Bearer test-only-secret'
        return Response()
    monkeypatch.setattr(restart_watchdog.urllib.request, 'urlopen', urlopen)
    assert restart_watchdog.mantis_healthy()
