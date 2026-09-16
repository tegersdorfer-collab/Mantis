"""Tests für den News-Globus-Aggregator (core/news_globe.py) — Quellen gemockt.

Gemockt: feeds.fetch_all, geolocate.geolocate_item, und der Cache-Speicher
(_write_cache/_read_cache) als In-Memory-Store. Geprüft: refresh schreibt Cache,
cached_geo filtert auf Items mit Koordinaten.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import news_globe
from tools.news import feeds, geolocate

# Echte Implementierungen beim Import sichern — andere Testmodule patchen
# cached/cached_geo global; hier stellen wir sie wieder her (Test-Isolation).
_REAL_CACHED = news_globe.cached
_REAL_CACHED_GEO = news_globe.cached_geo


def _setup(raw_items, geo_map):
    store = {"data": []}
    news_globe.cached = _REAL_CACHED
    news_globe.cached_geo = _REAL_CACHED_GEO
    news_globe._write_cache = lambda items: store.__setitem__("data", items)
    news_globe._read_cache = lambda: store["data"]

    async def fake_fetch_all(urls):
        return list(raw_items)
    feeds.fetch_all = fake_fetch_all

    async def fake_geo(item):
        out = dict(item)
        coords = geo_map.get(item["title"])
        if coords:
            out["place"], out["lat"], out["lon"] = coords
        return out
    geolocate.geolocate_item = fake_geo
    return store


def test_refresh_writes_and_locates():
    _setup(
        [{"title": "Beben Tokio", "link": "a"}, {"title": "Ohne Ort", "link": "b"}],
        {"Beben Tokio": ("Tokio", 35.68, 139.69)},
    )
    out = asyncio.run(news_globe.refresh({"feeds": ["u1"], "topics": []}))
    assert len(out) == 2
    assert news_globe.cached()[0]["title"] == "Beben Tokio"


def test_cached_geo_filters_located_only():
    _setup(
        [{"title": "Beben Tokio", "link": "a"}, {"title": "Ohne Ort", "link": "b"}],
        {"Beben Tokio": ("Tokio", 35.68, 139.69)},
    )
    asyncio.run(news_globe.refresh({"feeds": ["u1"], "topics": []}))
    geo = news_globe.cached_geo()
    assert len(geo) == 1 and geo[0]["place"] == "Tokio" and geo[0]["lat"] == 35.68


def test_refresh_adds_google_news_feed_for_configured_topic(monkeypatch):
    gesehen = []

    async def fake_fetch_all(urls):
        gesehen.extend(urls)
        return []

    monkeypatch.setattr(feeds, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(news_globe, "_write_cache", lambda items: None)

    asyncio.run(news_globe.refresh({"feeds": ["u1"], "topics": ["Quantencomputer"]}))

    assert gesehen == [
        "u1",
        "https://news.google.com/rss/search?q=Quantencomputer&hl=de&gl=DE&ceid=DE%3Ade",
    ]
