"""Retry- und Host-Regeln für den zweiten uvicorn-Server."""

import asyncio

import pytest

from web import gym_server


def _patch_uvicorn(monkeypatch, server_factory):
    monkeypatch.setattr(
        gym_server.uvicorn,
        "Config",
        lambda app, **kwargs: {"app": app, **kwargs},
    )
    monkeypatch.setattr(gym_server.uvicorn, "Server", server_factory)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5"])
def test_invalid_host_logs_and_never_starts_server(host, monkeypatch, caplog):
    starts = []
    _patch_uvicorn(monkeypatch, lambda *_args, **_kwargs: starts.append(True))

    asyncio.run(gym_server.run_gym_server(host, 7782, attempts=1))

    assert starts == []
    assert "error" in caplog.text.lower()


def test_loopback_and_tailscale_hosts_are_accepted():
    assert gym_server.validate_host("127.0.0.1") == "127.0.0.1"
    assert gym_server.validate_host("100.107.172.123") == "100.107.172.123"
    assert gym_server.validate_host("fd7a:115c:a1e0::123") == "fd7a:115c:a1e0::123"


@pytest.mark.parametrize("failure", [SystemExit, OSError])
def test_bind_failures_retry_exactly_attempts_times(failure, monkeypatch, caplog):
    starts = []
    delays = []

    class FailingServer:
        def __init__(self, _config):
            starts.append(True)
            self.should_exit = False

        async def serve(self):
            raise failure("bind failed")

    async def fake_sleep(delay):
        delays.append(delay)

    _patch_uvicorn(monkeypatch, FailingServer)
    monkeypatch.setattr(gym_server.asyncio, "sleep", fake_sleep)

    asyncio.run(gym_server.run_gym_server(
        "127.0.0.1", 7782, attempts=3, base_delay=0.5, max_delay=1,
    ))

    assert len(starts) == 3
    assert delays == [0.5, 1]
    assert "warning" in caplog.text.lower()


def test_success_after_first_bind_failure_stops_retrying(monkeypatch):
    starts = []
    delays = []

    class SecondAttemptServer:
        def __init__(self, _config):
            starts.append(True)
            self.should_exit = False

        async def serve(self):
            if len(starts) == 1:
                raise OSError("address not ready")
            self.should_exit = True

    async def fake_sleep(delay):
        delays.append(delay)

    _patch_uvicorn(monkeypatch, SecondAttemptServer)
    monkeypatch.setattr(gym_server.asyncio, "sleep", fake_sleep)

    asyncio.run(gym_server.run_gym_server(
        "127.0.0.1", 7782, attempts=4, base_delay=0.25,
    ))

    assert len(starts) == 2
    assert delays == [0.25]


def test_unexpected_normal_return_without_should_exit_is_retried(monkeypatch):
    starts = []
    delays = []

    class ReturnedServer:
        def __init__(self, _config):
            starts.append(True)
            self.should_exit = False

        async def serve(self):
            return None

    async def fake_sleep(delay):
        delays.append(delay)

    _patch_uvicorn(monkeypatch, ReturnedServer)
    monkeypatch.setattr(gym_server.asyncio, "sleep", fake_sleep)

    asyncio.run(gym_server.run_gym_server(
        "127.0.0.1", 7782, attempts=2, base_delay=0.1,
    ))

    assert len(starts) == 2
    assert delays == [0.1]


def test_backoff_remains_capped_after_many_retries(monkeypatch):
    delays = []

    class FailingServer:
        should_exit = False

        def __init__(self, _config):
            pass

        async def serve(self):
            raise OSError("bind failed")

    async def fake_sleep(delay):
        delays.append(delay)

    _patch_uvicorn(monkeypatch, FailingServer)
    monkeypatch.setattr(gym_server.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gym_server.log, "warning", lambda *_args, **_kwargs: None)

    asyncio.run(gym_server.run_gym_server(
        "127.0.0.1", 7782, attempts=1100, base_delay=5.0, max_delay=300.0,
    ))

    assert len(delays) == 1099
    assert delays[-1] == 300
