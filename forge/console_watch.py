"""Überwacht die TypeSafe-Console und meldet die Rückkehr per Telegram.

Der Watcher sendet nur beim Zustandswechsel von offline zu online. Ein
fehlgeschlagener Telegram-Versand lässt den Offline-Zustand bestehen, damit
die nächste Prüfung die Meldung erneut versucht.
"""
from __future__ import annotations

import http.client
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from forge import daemon, melden

log = logging.getLogger(__name__)

CONSOLE_URL = "https://console.typesafe.ai"
CHECK_INTERVAL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 15.0
STATE_FILE = Path("/tmp/mantis_typesafe_console_watch.state")
_ONLINE = "online"
_OFFLINE = "offline"


def console_online(url: str = CONSOLE_URL, timeout: float = REQUEST_TIMEOUT_SECONDS) -> bool:
    """Gibt zurück, ob die Console eine erfolgreiche HTTP-Antwort liefert."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mantis-TypeSafe-Console-Watch/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            return 200 <= status < 400
    except (http.client.HTTPException, OSError, TimeoutError, urllib.error.URLError):
        return False


def _read_state(path: Path) -> str | None:
    try:
        state = path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Console-Watcher: Statusdatei nicht lesbar (%s)", type(exc).__name__)
        return None
    return state if state in {_ONLINE, _OFFLINE} else None


def _write_state(path: Path, online: bool) -> bool:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(f"{_ONLINE if online else _OFFLINE}\n")
        temporary.replace(path)
        return True
    except OSError as exc:
        log.warning("Console-Watcher: Statusdatei nicht schreibbar (%s)", type(exc).__name__)
        temporary.unlink(missing_ok=True)
        return False


def poll_once(
    url: str,
    state_path: Path,
    send: Callable[[str], bool] = melden.sende,
) -> bool:
    """Prüft einmal und meldet ausschließlich offline → online."""
    previous = _read_state(state_path)
    online = console_online(url, REQUEST_TIMEOUT_SECONDS)
    if online and previous == _OFFLINE:
        message = f"✅ TypeSafe Console ist wieder online: {url}"
        try:
            delivered = send(message)
        except Exception as exc:  # pragma: no cover - defensive boundary
            log.warning("Console-Watcher: Telegram-Versand fehlgeschlagen (%s)", type(exc).__name__)
            delivered = False
        if not delivered:
            return online
    _write_state(state_path, online)
    return online


def _float_from_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    daemon.lade_api_schluessel(daemon.API_SCHLUESSEL_DATEI)
    daemon.lade_api_schluessel(daemon.ENV_DATEI, nur=daemon.ENV_NUR)
    url = os.environ.get("TYPESAFE_CONSOLE_URL", CONSOLE_URL).strip() or CONSOLE_URL
    state_path = Path(os.environ.get("TYPESAFE_CONSOLE_WATCH_STATE", str(STATE_FILE)))
    interval = _float_from_env("TYPESAFE_CONSOLE_WATCH_INTERVAL", CHECK_INTERVAL_SECONDS)
    log.info("Console-Watcher gestartet: %s", url)
    while True:
        status = "online" if poll_once(url, state_path) else "offline"
        log.info("TypeSafe Console: %s", status)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
