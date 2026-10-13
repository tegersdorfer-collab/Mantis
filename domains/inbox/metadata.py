"""Begrenzter, metadatenbasierter Abruf öffentlicher Inbox-Links."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from domains.inbox.capture import platform_for_url


_METADATA_TIMEOUT_S = 8.0
_MAX_HTML_BYTES = 512 * 1024
_MAX_METADATA_BYTES = 1_000_000
_http_client_factory = httpx.AsyncClient


class _HeadMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self._title_depth = 0
        self._title: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(key).casefold(): value for key, value in attrs}
        if tag.casefold() == "title":
            self._title_depth += 1
        if tag.casefold() == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").casefold()
            content = (attrs.get("content") or "").strip()
            aliases = {
                "og:title": "title", "twitter:title": "title",
                "description": "description", "og:description": "description",
                "twitter:description": "description",
                "author": "author", "article:author": "author",
                "og:image": "thumbnail", "twitter:image": "thumbnail",
                "article:published_time": "published_at", "datepublished": "published_at",
            }
            field = aliases.get(key)
            if field and content and (field not in self.values or key.startswith("og:")):
                self.values[field] = content

    def handle_endtag(self, tag):
        if tag.casefold() == "title" and self._title_depth:
            self._title_depth -= 1
            value = " ".join("".join(self._title).split())
            if value and not self.values.get("title"):
                self.values["title"] = value
            self._title.clear()

    def handle_data(self, data):
        if self._title_depth:
            self._title.append(data)


def _safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme.casefold() not in {"http", "https"} or not host:
            return False
        if parsed.username or parsed.password:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".example")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return address.is_global
    except (TypeError, ValueError):
        return False


async def fetch_metadata(url: str, *, timeout_s: float = _METADATA_TIMEOUT_S) -> dict:
    """Holt nur Titel-/Quellenmetadaten; ungültige URLs und Fehler liefern `{}`."""
    if not _safe_public_url(url):
        return {}
    budget = min(_METADATA_TIMEOUT_S, max(0.01, float(timeout_s)))
    platform = platform_for_url(url)
    try:
        operation = _fetch_ytdlp(url, platform, budget) if platform in {
            "youtube", "tiktok", "instagram"
        } else _fetch_article(url, platform, budget)
        return await asyncio.wait_for(operation, timeout=budget)
    except (asyncio.TimeoutError, httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return {}
    except Exception:
        # Fremde Webseiten und yt-dlp-Versionen liefern uneinheitliche Fehler.
        # Fehlertexte können vollständige URLs enthalten und werden nicht geloggt.
        return {}


async def _fetch_ytdlp(url: str, platform: str, timeout_s: float) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--dump-single-json",
        "--skip-download", "--no-warnings", "--no-playlist", "--socket-timeout", "4",
        "--no-progress", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=max(0.001, timeout_s * 0.8)
        )
    except asyncio.TimeoutError:
        proc.kill()
        wait = getattr(proc, "wait", None)
        if wait:
            try:
                await asyncio.wait_for(wait(), timeout=0.25)
            except Exception:
                pass
        return {}
    if proc.returncode not in {0, None} or len(stdout) > _MAX_METADATA_BYTES:
        return {}
    data = json.loads(stdout.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        return {}
    title = _clean(data.get("title"), 500)
    description = _clean(data.get("description"), 2500)
    if not title and not description:
        return {}
    return {
        "type": "video",
        "title": title,
        "description": description,
        "author": _clean(data.get("uploader") or data.get("channel"), 250),
        "platform": platform,
        "thumbnail": _public_media_url(data.get("thumbnail")),
        "published_at": _clean(data.get("upload_date") or data.get("release_date"), 40) or None,
    }


async def _fetch_article(url: str, platform: str, timeout_s: float) -> dict:
    current = url
    async with _http_client_factory(
        timeout=httpx.Timeout(min(timeout_s, 5.0)),
        follow_redirects=False,
        headers={"User-Agent": "Mantis-Inbox/1.0", "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        for _ in range(4):
            if not _safe_public_url(current):
                return {}
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return {}
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "html" not in content_type:
                    return {}
                if int(response.headers.get("content-length") or 0) > _MAX_HTML_BYTES:
                    return {}
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_HTML_BYTES:
                        return {}
                    chunks.append(chunk)
                html = b"".join(chunks).decode("utf-8", errors="replace")
                parser = _HeadMetadataParser()
                parser.feed(html)
                values = parser.values
                if not values.get("title") and not values.get("description"):
                    return {}
                return {
                    "type": "article",
                    "title": _clean(values.get("title"), 500),
                    "description": _clean(values.get("description"), 2500),
                    "author": _clean(values.get("author"), 250),
                    "platform": platform,
                    "thumbnail": _public_media_url(values.get("thumbnail")),
                    "published_at": _clean(values.get("published_at"), 40) or None,
                }
    return {}


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _public_media_url(value) -> str | None:
    text = _clean(value, 2000)
    return text if text and _safe_public_url(text) else None
