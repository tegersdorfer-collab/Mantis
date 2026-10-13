"""Offline-Tests für begrenzte Link-Metadaten ohne Medien- oder Volltextabruf."""

import asyncio
import json

import httpx
import pytest

from domains.inbox import metadata


def test_ytdlp_metadata_uses_no_download_and_discards_transcript_fields(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({
                "title": "Video Titel",
                "description": "Kurzbeschreibung",
                "uploader": "Kanal",
                "thumbnail": "https://i.example.org/thumb.jpg",
                "upload_date": "20261014",
                "requested_downloads": [{"filepath": "/private/video.mp4"}],
                "subtitles": {"de": [{"url": "https://caption.example/file.vtt"}]},
            }).encode(), b""

        def kill(self):
            calls.append("killed")

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(metadata.fetch_metadata("https://youtu.be/abc123"))

    argv = calls[0]
    assert "--skip-download" in argv
    assert "--dump-single-json" in argv
    assert "--no-playlist" in argv
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


def test_ytdlp_timeout_kills_child_process(monkeypatch):
    state = {"killed": False}

    class Process:
        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            state["killed"] = True

    async def fake_exec(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(metadata.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(metadata, "_METADATA_TIMEOUT_S", 0.1)
    result = asyncio.run(metadata.fetch_metadata("https://www.tiktok.com/@a/video/1"))

    assert result == {}
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

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(metadata, "_http_client_factory", client_factory)
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

    monkeypatch.setattr(
        metadata,
        "_http_client_factory",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = asyncio.run(metadata.fetch_metadata("https://example.org/start"))

    assert result == {}
    assert requested == ["https://example.org/start"]
