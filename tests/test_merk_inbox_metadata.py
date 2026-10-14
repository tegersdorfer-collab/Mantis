"""Offline-Tests für begrenzte Link-Metadaten ohne Medien- oder Volltextabruf."""

import asyncio
import json
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from domains.inbox import metadata


class _OutputStream:
    def __init__(self, data=b"", *, gate=None, read_sizes=None):
        self.data = data
        self.gate = gate
        self.read_sizes = read_sizes if read_sizes is not None else []

    async def read(self, size):
        if self.gate is not None:
            await self.gate.wait()
        chunk = self.data[:size]
        self.data = self.data[len(chunk):]
        self.read_sizes.append(len(chunk))
        return chunk


def _public_dns(monkeypatch):
    async def resolve(_host, _port):
        return ["93.184.216.34"]

    monkeypatch.setattr(metadata, "_resolve_host", resolve, raising=False)


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        metadata, "_http_transport_factory",
        lambda *_args: httpx.MockTransport(handler), raising=False,
    )
    monkeypatch.setattr(metadata, "_http_client_factory", httpx.AsyncClient)


def test_ytdlp_metadata_uses_no_download_and_discards_transcript_fields(monkeypatch):
    calls = []

    class Process:
        returncode = 0
        stdout = _OutputStream(json.dumps({
            "title": "Video Titel",
            "description": "Kurzbeschreibung",
            "uploader": "Kanal",
            "thumbnail": "https://i.example.org/thumb.jpg",
            "upload_date": "20261014",
            "requested_downloads": [{"filepath": "/private/video.mp4"}],
            "subtitles": {"de": [{"url": "https://caption.example/file.vtt"}]},
        }).encode())
        stderr = _OutputStream()

        async def wait(self):
            return 0

        def kill(self):
            calls.append("killed")

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return Process()

    _public_dns(monkeypatch)
    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(metadata.fetch_metadata("https://youtu.be/abc123"))

    argv = calls[0]
    assert "--skip-download" in argv
    assert "--dump-single-json" in argv
    assert "--no-playlist" in argv
    assert "--skip-download" in argv
    assert result == {
        "type": "video",
        "title": "Video Titel",
        "description": "Kurzbeschreibung",
        "author": "Kanal",
        "platform": "youtube",
        "thumbnail": "https://i.example.org/thumb.jpg",
        "published_at": "20261014",
    }
    assert "requested_downloads" not in result
    assert "subtitles" not in result


@pytest.mark.parametrize("timeout", ["not-a-number", None])
def test_invalid_metadata_timeout_returns_empty_result(timeout):
    assert asyncio.run(metadata.fetch_metadata("https://example.org", timeout_s=timeout)) == {}


def test_ytdlp_timeout_kills_child_process(monkeypatch):
    state = {"killed": False}
    done = asyncio.Event()

    class Process:
        returncode = None
        stdout = _OutputStream(gate=done)
        stderr = _OutputStream(gate=done)

        async def wait(self):
            await done.wait()
            self.returncode = -9
            return self.returncode

        def kill(self):
            state["killed"] = True
            self.returncode = -9
            done.set()

    async def fake_exec(*_args, **_kwargs):
        return Process()

    _public_dns(monkeypatch)
    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(metadata, "_METADATA_TIMEOUT_S", 0.1)
    result = asyncio.run(metadata.fetch_metadata("https://www.tiktok.com/@a/video/1"))

    assert result == {}
    assert state["killed"] is True


def test_ytdlp_timeout_kills_process_group_when_leader_already_exited(monkeypatch, tmp_path):
    marker = tmp_path / "child-survived-timeout"
    child = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    bootstrap = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('{}', flush=True)"
    )
    _public_dns(monkeypatch)
    monkeypatch.setattr(metadata, "_YTDLP_BOOTSTRAP", bootstrap)
    monkeypatch.setattr(metadata, "_METADATA_TIMEOUT_S", 0.25)

    result = asyncio.run(metadata.fetch_metadata("https://youtu.be/abc123"))
    time.sleep(0.9)

    assert result == {}
    assert not marker.exists()


def test_ytdlp_output_is_capped_while_reading_and_process_is_killed(monkeypatch):
    state = {"killed": False}
    stdout_sizes = []

    class Process:
        returncode = None
        stdout = _OutputStream(b"x" * 200, read_sizes=stdout_sizes)
        stderr = _OutputStream()

        async def wait(self):
            return self.returncode or 0

        def kill(self):
            state["killed"] = True
            self.returncode = -9

    async def fake_exec(*_args, **_kwargs):
        return Process()

    _public_dns(monkeypatch)
    monkeypatch.setattr(metadata, "_MAX_METADATA_BYTES", 32)
    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(metadata.fetch_metadata("https://youtu.be/abc123"))

    assert result == {}
    assert sum(stdout_sizes) <= 33
    assert state["killed"] is True


def test_delayed_ytdlp_start_is_killed_if_outer_timeout_wins(monkeypatch):
    state = {"started": False, "killed": False}
    release = asyncio.Event()

    class Process:
        returncode = None
        stdout = _OutputStream()
        stderr = _OutputStream()

        async def wait(self):
            return self.returncode or -9

        def kill(self):
            state["killed"] = True
            self.returncode = -9

    process = Process()

    async def delayed_exec(*_args, **_kwargs):
        state["started"] = True
        await release.wait()
        return process

    async def run():
        monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", delayed_exec)
        task = asyncio.create_task(metadata._fetch_ytdlp(
            "https://youtu.be/abc123", "youtube", 0.1,
            addresses=("93.184.216.34",),
        ))
        while not state["started"]:
            await asyncio.sleep(0)
        result = await task
        release.set()
        await asyncio.sleep(0)
        assert result == {}

    asyncio.run(run())

    assert state["killed"] is True


def test_article_extracts_only_html_metadata_and_follows_safe_redirect(monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/article"})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=(
                '<html><head><title>Artikel Titel</title>'
                '<meta property="og:description" content="Kurzinfo">'
                '<meta name="author" content="Autorin"></head>'
                '<body><p>Dieser Volltext darf nicht gespeichert werden.</p></body></html>'
            ),
        )

    _public_dns(monkeypatch)
    _mock_http(monkeypatch, respond)
    result = asyncio.run(metadata.fetch_metadata("https://example.org/start"))

    assert requested == ["https://example.org/start", "https://example.org/article"]
    assert result == {
        "type": "article",
        "title": "Artikel Titel",
        "description": "Kurzinfo",
        "author": "Autorin",
        "platform": "article",
        "thumbnail": None,
        "published_at": None,
    }
    assert "Dieser Volltext" not in str(result)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:pass@example.org/article",
    "http://127.0.0.1/private",
    "http://localhost/private",
    "https://example.org.evil.invalid/article",
])
def test_invalid_or_private_url_is_rejected_before_network(monkeypatch, url):
    called = []
    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", lambda *a, **k: called.append(a))
    monkeypatch.setattr(metadata, "_http_client_factory", lambda **kwargs: called.append(kwargs))

    result = asyncio.run(metadata.fetch_metadata(url))

    assert result == {}
    assert called == []


def test_redirect_to_private_host_is_rejected(monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    _public_dns(monkeypatch)
    _mock_http(monkeypatch, respond)
    result = asyncio.run(metadata.fetch_metadata("https://example.org/start"))

    assert result == {}
    assert requested == ["https://example.org/start"]


@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f000001"])
def test_noncanonical_numeric_loopback_hosts_are_rejected(monkeypatch, host):
    clients = []
    monkeypatch.setattr(metadata, "_http_client_factory", lambda **kwargs: clients.append(kwargs))

    result = asyncio.run(metadata.fetch_metadata(f"http://{host}/private"))

    assert result == {}
    assert clients == []


def test_dns_hostname_resolving_to_private_address_is_rejected(monkeypatch):
    clients = []

    async def resolve(_host, _port):
        return ["127.0.0.1"]

    monkeypatch.setattr(metadata, "_resolve_host", resolve, raising=False)
    monkeypatch.setattr(metadata, "_http_client_factory", lambda **kwargs: clients.append(kwargs))

    result = asyncio.run(metadata.fetch_metadata("https://public-looking.example.com/article"))

    assert result == {}
    assert clients == []


def test_every_dns_answer_must_be_global_before_connecting(monkeypatch):
    clients = []

    async def resolve(_host, _port):
        return ["93.184.216.34", "10.0.0.7"]

    monkeypatch.setattr(metadata, "_resolve_host", resolve, raising=False)
    monkeypatch.setattr(metadata, "_http_client_factory", lambda **kwargs: clients.append(kwargs))

    result = asyncio.run(metadata.fetch_metadata("https://public-looking.example.com/article"))

    assert result == {}
    assert clients == []


def test_article_redirect_hostname_is_resolved_again_before_request(monkeypatch):
    requested = []

    async def resolve(host, _port):
        return ["93.184.216.34"] if host == "example.org" else ["127.0.0.1"]

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://private-looking.example.net/admin"})

    monkeypatch.setattr(metadata, "_resolve_host", resolve, raising=False)
    _mock_http(monkeypatch, respond)

    result = asyncio.run(metadata.fetch_metadata("https://example.org/start"))

    assert result == {}
    assert requested == ["https://example.org/start"]


def test_ytdlp_is_limited_to_exact_platform_hosts(monkeypatch):
    launched = []
    _public_dns(monkeypatch)
    monkeypatch.setattr(
        metadata.asyncio, "create_subprocess_exec",
        lambda *args, **_kwargs: launched.append(args),
    )

    result = asyncio.run(metadata.fetch_metadata("https://attacker.youtube.com/watch?v=1"))

    assert result == {}
    assert launched == []


@pytest.mark.parametrize(("url", "platform"), [
    ("https://youtube.com/watch?v=1", "youtube"),
    ("https://youtu.be/1", "youtube"),
    ("https://m.youtube.com/watch?v=1", "youtube"),
    ("https://www.youtube.com/watch?v=1", "youtube"),
    ("https://WWW.YouTube.com./watch?v=1", "youtube"),
    ("https://music.youtube.com/watch?v=1", "youtube"),
    ("https://tiktok.com/@a/video/1", "tiktok"),
    ("https://vm.tiktok.com/1", "tiktok"),
    ("https://www.tiktok.com/@a/video/1", "tiktok"),
    ("https://instagram.com/reel/1", "instagram"),
    ("https://www.instagram.com/reel/1", "instagram"),
])
def test_ytdlp_allowlist_accepts_only_documented_platform_hosts(url, platform):
    assert metadata._ytdlp_host_allowed(url, platform) is True


def test_www_youtube_metadata_uses_ytdlp(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(metadata, "_fetch_ytdlp", AsyncMock(return_value={"title": "Video"}))

    result = asyncio.run(metadata.fetch_metadata("https://www.youtube.com/watch?v=abc"))

    assert result == {"title": "Video"}
    assert metadata._fetch_ytdlp.await_args.args == (
        "https://www.youtube.com/watch?v=abc", "youtube", metadata._METADATA_TIMEOUT_S,
    )


def test_ytdlp_process_resolver_rejects_private_and_mixed_dns_answers(monkeypatch):
    public = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))
    private = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.8", 443))
    answers = {
        "public.example": [public],
        "mixed.example": [public, private],
        "2130706433": [private],
        "127.1": [private],
        "0x7f000001": [private],
    }
    fake_socket = SimpleNamespace(
        getaddrinfo=lambda host, port, *args, **kwargs: answers[host],
    )
    fake_runpy = SimpleNamespace(run_module=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "socket", fake_socket)
    monkeypatch.setitem(sys.modules, "runpy", fake_runpy)

    exec(metadata._YTDLP_BOOTSTRAP, {})

    assert fake_socket.getaddrinfo("public.example", 443) == [public]
    with pytest.raises(OSError):
        fake_socket.getaddrinfo("mixed.example", 443)
    for host in ("2130706433", "127.1", "0x7f000001"):
        with pytest.raises(OSError):
            fake_socket.getaddrinfo(host, 443)


def test_pinned_http_backend_connects_to_checked_ip_and_rejects_other_host():
    class Backend:
        def __init__(self):
            self.hosts = []

        async def connect_tcp(self, host, port, **_kwargs):
            self.hosts.append((host, port))
            return "connection"

        async def connect_unix_socket(self, *_args, **_kwargs):
            raise AssertionError("Unix-Socket darf nicht aufgerufen werden")

        async def sleep(self, _seconds):
            return None

    async def run():
        backend = Backend()
        pinned = metadata._PinnedNetworkBackend("example.org", ("93.184.216.34",), backend)

        assert await pinned.connect_tcp("example.org", 443) == "connection"
        assert backend.hosts == [("93.184.216.34", 443)]
        with pytest.raises(OSError):
            await pinned.connect_tcp("attacker.example", 443)

    asyncio.run(run())
