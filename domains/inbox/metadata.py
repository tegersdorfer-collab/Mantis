"""Begrenzter, metadatenbasierter Abruf öffentlicher Inbox-Links."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import signal
import socket
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from domains.inbox.capture import platform_for_url


_METADATA_TIMEOUT_S = 8.0
_MAX_HTML_BYTES = 512 * 1024
_MAX_METADATA_BYTES = 1_000_000
_MAX_STDERR_BYTES = 64 * 1024
_http_client_factory = httpx.AsyncClient
_http_transport_factory = None
_YTDLP_BOOTSTRAP = r"""
import ipaddress, runpy, socket
_original_getaddrinfo = socket.getaddrinfo
def _checked_getaddrinfo(host, port, *args, **kwargs):
    if not isinstance(host, str):
        return _original_getaddrinfo(host, port, *args, **kwargs)
    try:
        ascii_host = host.encode('idna').decode('ascii').casefold().rstrip('.')
    except (UnicodeError, AttributeError):
        raise OSError('invalid host')
    labels = ascii_host.split('.')
    if labels and all(label.isdecimal() or
                      (label.startswith('0x') and label[2:] and
                       all(char in '0123456789abcdef' for char in label[2:]))
                      for label in labels):
        raise OSError('noncanonical numeric host')
    records = _original_getaddrinfo(host, port, *args, **kwargs)
    if not records:
        raise OSError('host did not resolve')
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split('%', 1)[0])
        except ValueError as error:
            raise OSError('invalid resolved address') from error
        if not address.is_global:
            raise OSError('non-public resolved address')
    return records
socket.getaddrinfo = _checked_getaddrinfo
runpy.run_module('yt_dlp', run_name='__main__')
"""


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
        if "%" in host:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".example")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return not _is_noncanonical_numeric_host(host)
        return address.is_global
    except (TypeError, ValueError):
        return False


def _is_noncanonical_numeric_host(host: str) -> bool:
    """Verwirft Browser-IPv4-Kurzformen und Integer-/Hex-Schreibweisen."""
    try:
        ascii_host = host.encode("idna").decode("ascii").casefold()
    except (UnicodeError, AttributeError):
        return True
    labels = ascii_host.split(".")
    if not labels or any(not label for label in labels):
        return True
    return all(
        label.isdecimal() or label.startswith("0x") and _is_hex(label[2:])
        for label in labels
    )


def _is_hex(value: str) -> bool:
    return bool(value) and all(char in "0123456789abcdef" for char in value)


async def _resolve_host(host: str, port: int) -> list[str]:
    """Löst DNS im Eventloop auf; der Aufrufer prüft sämtliche Antworten."""
    records = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM,
    )
    return [record[4][0] for record in records]


async def _resolve_public_addresses(url: str) -> tuple[str, ...]:
    """Liefert nur ausschließlich globale, geprüfte Zieladressen zurück."""
    if not _safe_public_url(url):
        return ()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw_addresses = await _resolve_host(host, port)
        except (OSError, ValueError, asyncio.TimeoutError):
            return ()
    else:
        raw_addresses = [literal.compressed]

    addresses = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
        except ValueError:
            return ()
        if not address.is_global:
            return ()
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    return tuple(addresses)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Verbindet HTTPX nur mit einer IP, die zuvor für den Host geprüft wurde."""

    def __init__(self, host: str, addresses: tuple[str, ...], backend=None):
        self._host = host.casefold().rstrip(".")
        self._addresses = addresses
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None,
    ):
        if str(host).casefold().rstrip(".") != self._host:
            raise OSError("HTTPX-Zielhost stimmt nicht mit DNS-Prüfung überein")
        last_error = None
        for address in self._addresses:
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise OSError("Keine geprüfte globale IP-Adresse")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("Unix-Sockets sind für Inbox-Metadaten nicht erlaubt")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, host: str, addresses: tuple[str, ...]):
        super().__init__(trust_env=False, retries=0)
        self._pool._network_backend = _PinnedNetworkBackend(host, addresses)


def _make_pinned_transport(host: str, addresses: tuple[str, ...]):
    factory = _http_transport_factory or _PinnedHTTPTransport
    return factory(host, addresses)


def _ytdlp_host_allowed(url: str, platform: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").encode("idna").decode("ascii")
    except (UnicodeError, AttributeError):
        return False
    host = host.casefold().rstrip(".").removeprefix("www.")
    allowlist = {
        "youtube": {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"},
        "tiktok": {"tiktok.com", "vm.tiktok.com"},
        "instagram": {"instagram.com"},
    }.get(platform, ())
    return host in allowlist


async def fetch_metadata(url: str, *, timeout_s: float = _METADATA_TIMEOUT_S) -> dict:
    """Holt nur Titel-/Quellenmetadaten; ungültige URLs und Fehler liefern `{}`."""
    try:
        requested_timeout = float(timeout_s)
        if not math.isfinite(requested_timeout):
            return {}
        budget = min(_METADATA_TIMEOUT_S, max(0.01, requested_timeout))
        platform = platform_for_url(url)
    except (TypeError, ValueError, OverflowError):
        return {}

    async def fetch():
        addresses = await _resolve_public_addresses(url)
        if not addresses:
            return {}
        if platform in {"youtube", "tiktok", "instagram"}:
            if not _ytdlp_host_allowed(url, platform):
                return {}
            return await _fetch_ytdlp(url, platform, budget, addresses=addresses)
        return await _fetch_article(
            url, platform, budget, initial_addresses=addresses,
        )

    try:
        return await asyncio.wait_for(fetch(), timeout=budget)
    except (asyncio.TimeoutError, httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return {}
    except Exception:
        # Fremde Webseiten und yt-dlp-Versionen liefern uneinheitliche Fehler.
        # Fehlertexte können vollständige URLs enthalten und werden nicht geloggt.
        return {}


class _OutputTooLarge(Exception):
    """yt-dlp hat mehr Ausgabe produziert als für Metadaten erlaubt ist."""


async def _read_limited(stream, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(min(64 * 1024, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise _OutputTooLarge


def _ytdlp_env() -> dict[str, str]:
    proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    return {
        key: value for key, value in os.environ.items()
        if key.casefold() not in proxy_names
    }


def _kill_process_tree(proc) -> None:
    pid = getattr(proc, "pid", None)
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except OSError:
            pass
    kill = getattr(proc, "kill", None)
    if callable(kill):
        try:
            kill()
        except OSError:
            pass


def _terminate_created_process(task) -> None:
    try:
        proc = task.result()
    except BaseException:
        return
    _kill_process_tree(proc)
    try:
        asyncio.get_running_loop().create_task(_reap_process(proc))
    except RuntimeError:
        pass


async def _reap_process(proc) -> None:
    wait = getattr(proc, "wait", None)
    if callable(wait):
        try:
            await asyncio.wait_for(wait(), timeout=0.5)
        except Exception:
            pass


async def _fetch_ytdlp(
    url: str,
    platform: str,
    timeout_s: float,
    *,
    addresses: tuple[str, ...] | None = None,
) -> dict:
    if not _ytdlp_host_allowed(url, platform):
        return {}
    if addresses is None:
        addresses = await _resolve_public_addresses(url)
    if not addresses:
        return {}

    proc = None
    readers = []
    kill_process_tree = False
    try:
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-c", _YTDLP_BOOTSTRAP,
                "--ignore-config", "--dump-single-json", "--skip-download",
                "--no-warnings", "--no-playlist", "--socket-timeout", "4",
                "--no-progress", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_ytdlp_env(),
                start_new_session=True,
            ))
        try:
            proc = await asyncio.wait_for(
                asyncio.shield(creation), timeout=max(0.001, timeout_s * 0.2),
            )
        except asyncio.TimeoutError:
            creation.add_done_callback(_terminate_created_process)
            return {}
        except asyncio.CancelledError:
            creation.add_done_callback(_terminate_created_process)
            raise
        stdout_task = asyncio.create_task(_read_limited(proc.stdout, _MAX_METADATA_BYTES))
        stderr_task = asyncio.create_task(_read_limited(proc.stderr, _MAX_STDERR_BYTES))
        wait_task = asyncio.create_task(proc.wait())
        readers = [stdout_task, stderr_task, wait_task]
        stdout, _stderr, returncode = await asyncio.wait_for(
            asyncio.gather(*readers), timeout=max(0.001, timeout_s * 0.8),
        )
    except (asyncio.TimeoutError, _OutputTooLarge, OSError, ValueError):
        kill_process_tree = True
        return {}
    except asyncio.CancelledError:
        kill_process_tree = True
        raise
    finally:
        if proc is not None and (
            kill_process_tree or getattr(proc, "returncode", None) is None
        ):
            _kill_process_tree(proc)
        for task in readers:
            if not task.done():
                task.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if proc is not None:
            await _reap_process(proc)

    if returncode != 0:
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


async def _fetch_article(
    url: str,
    platform: str,
    timeout_s: float,
    *,
    initial_addresses: tuple[str, ...] | None = None,
) -> dict:
    current = url
    for redirect_count in range(4):
        addresses = (
            initial_addresses if redirect_count == 0 and initial_addresses is not None
            else await _resolve_public_addresses(current)
        )
        if not addresses:
            return {}
        host = (urlparse(current).hostname or "").casefold().rstrip(".")
        async with _http_client_factory(
            transport=_make_pinned_transport(host, addresses),
            timeout=httpx.Timeout(min(timeout_s, 5.0)),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "Mantis-Inbox/1.0", "Accept": "text/html,application/xhtml+xml"},
        ) as client:
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
