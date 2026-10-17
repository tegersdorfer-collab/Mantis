"""Strukturelle Vertragstests für die installierbare Gym-Oberfläche."""

import json
from pathlib import Path
import re


GYM_DIR = Path(__file__).resolve().parents[1] / "web" / "gym"


def test_shell_links_assets_and_ios_install_metadata():
    index = (GYM_DIR / "index.html").read_text(encoding="utf-8")

    assert "app.js" in index
    assert "app.css" in index
    assert "manifest.webmanifest" in index
    assert 'name="apple-mobile-web-app-capable" content="yes"' in index
    assert 'rel="apple-touch-icon"' in index


def test_manifest_opens_the_full_screen_app_under_old():
    manifest = json.loads((GYM_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/old/"
    assert manifest["scope"] == "/old/"


def test_frontend_uses_idempotent_offline_queue_and_rest_timer():
    script = GYM_DIR / "app.js"
    assert script.is_file()
    app_js = script.read_text(encoding="utf-8")

    for contract in (
        "crypto.randomUUID",
        "gym_queue",
        "/api/sets",
        "flushQueue",
        "gym_timer_end",
        "navigator.vibrate",
    ):
        assert contract in app_js


def test_layout_accounts_for_safe_area_and_touch_targets():
    stylesheet = GYM_DIR / "app.css"
    assert stylesheet.is_file()
    app_css = stylesheet.read_text(encoding="utf-8")

    assert "env(safe-area-inset-top)" in app_css
    assert "env(safe-area-inset-bottom)" in app_css
    assert "min-height: 48px" in app_css
    measure = re.search(r"\.measure-control\s*\{([^}]*)\}", app_css)
    timer = re.search(r"\.timer-dock \.small-button\s*\{([^}]*)\}", app_css)
    assert ".measure-control button" not in app_css
    assert measure and "min-height: 54px" in measure.group(1)
    assert measure and "font-size: 1.42rem" in measure.group(1)
    assert timer and "min-width: 48px" in timer.group(1)
    assert timer and "min-height: 48px" in timer.group(1)
