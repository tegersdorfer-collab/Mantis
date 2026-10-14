"""Tests für die News-Geolokalisierung (tools/news/geolocate.py) — ohne LLM/Netz.

Gemockt: _extract_place (Fast-Cloud-Route) und nominatim.geocode. Geprüft: Item bekommt
Koordinaten, wird ohne Ort/ohne Geocode-Treffer korrekt übersprungen, Cache greift.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.news import geolocate
from tools.geo import nominatim


def _setup(place, geo, monkeypatch):
    monkeypatch.setattr(geolocate, "_cache", {})
    monkeypatch.setattr(geolocate, "_save_cache", lambda: None)
    calls = {"extract": 0}

    async def fake_extract(text):
        calls["extract"] += 1
        return place
    monkeypatch.setattr(geolocate, "_extract_place", fake_extract)

    async def fake_geocode(p):
        return geo
    monkeypatch.setattr(nominatim, "geocode", fake_geocode)
    return calls


def test_item_gets_coordinates(monkeypatch):
    _setup("Tokio", {"lat": 35.68, "lon": 139.69, "display_name": "Tokio"}, monkeypatch)
    item = {"title": "Erdbeben in Tokio", "summary": ""}
    out = asyncio.run(geolocate.geolocate_item(item))
    assert out["lat"] == 35.68 and out["lon"] == 139.69 and out["place"] == "Tokio"


def test_item_without_place_has_no_coords(monkeypatch):
    _setup("", None, monkeypatch)
    out = asyncio.run(geolocate.geolocate_item({"title": "Allgemeine Meldung", "summary": ""}))
    assert "lat" not in out and "place" not in out


def test_item_place_but_no_geocode(monkeypatch):
    _setup("Nirgendwo", None, monkeypatch)   # Ort erkannt, aber kein Geocode-Treffer
    out = asyncio.run(geolocate.geolocate_item({"title": "X in Nirgendwo", "summary": ""}))
    assert "lat" not in out


def test_locate_headline_caches(monkeypatch):
    calls = _setup("Paris", {"lat": 48.85, "lon": 2.35, "display_name": "Paris"}, monkeypatch)
    asyncio.run(geolocate.locate_headline("Wahl in Paris"))
    asyncio.run(geolocate.locate_headline("wahl in paris"))   # gleiche Schlagzeile
    assert calls["extract"] == 1


def test_extract_place_uses_fast_cloud_route_without_model_override(monkeypatch):
    calls = []

    async def fake_ask(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "Paris"

    monkeypatch.setattr(geolocate.fast, "ask", fake_ask)

    assert asyncio.run(geolocate._extract_place("Wahl in Paris")) == "Paris"
    assert calls[0][1] == {"max_tokens": 12}
