import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from forge import console_watch


class _Response(io.BytesIO):
    def __init__(self, status):
        super().__init__(b"ok")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_online_console_is_detected_without_reading_page(monkeypatch):
    gesehen = []

    def _urlopen(request, timeout):
        gesehen.append((request.full_url, timeout))
        return _Response(200)

    monkeypatch.setattr(console_watch.urllib.request, "urlopen", _urlopen)

    assert console_watch.console_online("https://console.typesafe.ai", timeout=7) is True
    assert gesehen == [("https://console.typesafe.ai", 7)]


def test_offline_to_online_sends_one_message_and_updates_state(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.write_text("offline\n")
    monkeypatch.setattr(console_watch, "console_online", lambda url, timeout: True)
    gesendet = []

    def _send(text):
        gesendet.append(text)
        return True

    assert console_watch.poll_once(
        "https://console.typesafe.ai", state, send=_send
    ) is True
    assert gesendet == ["✅ TypeSafe Console ist wieder online: https://console.typesafe.ai"]
    assert state.read_text() == "online\n"


def test_initial_online_does_not_send(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr(console_watch, "console_online", lambda url, timeout: True)
    gesendet = []

    assert console_watch.poll_once("https://console.typesafe.ai", state, send=gesendet.append) is True
    assert gesendet == []
    assert state.read_text() == "online\n"


def test_failed_telegram_delivery_keeps_offline_state_for_retry(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.write_text("offline\n")
    monkeypatch.setattr(console_watch, "console_online", lambda url, timeout: True)

    assert console_watch.poll_once("https://console.typesafe.ai", state, send=lambda text: False) is True
    assert state.read_text() == "offline\n"


def test_http_failure_is_offline(monkeypatch):
    def _urlopen(request, timeout):
        raise console_watch.urllib.error.URLError("offline")

    monkeypatch.setattr(console_watch.urllib.request, "urlopen", _urlopen)

    assert console_watch.console_online("https://console.typesafe.ai", timeout=7) is False
