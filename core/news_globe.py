"""News-Globus-Aggregator — Feeds holen, geolokalisieren, cachen.

refresh() läuft im Idle-Loop periodisch: es aggregiert die konfigurierten Feeds,
lokalisiert jede Schlagzeile (Ort → Koordinate) und schreibt das Ergebnis in
data/news_cache.json. Das Dashboard liest nur den Cache (cached_geo) → lädt sofort.

Sequentielle Geolokalisierung ist gewollt: sie respektiert Nominatims Rate-Limit
(Geocode ist zudem gecacht, spätere Läufe sind schnell).
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode

from tools.news import feeds, geolocate

log = logging.getLogger("core.news")

_MAX_ITEMS = 60  # Deckel gegen zu viele LLM-/Geocode-Calls pro Lauf
_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "news_cache.json")
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "news_sources.json")
_TOPIC_FEED_URL = "https://news.google.com/rss/search"

# Sinnvolle Defaults, falls data/news_sources.json fehlt (data/ ist gitignored,
# also greift das out-of-the-box). Timo kann die Datei anlegen, um zu überschreiben.
_DEFAULT_CONFIG = {
    "feeds": [
        "https://www.tagesschau.de/index~rss2.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.reutersagency.com/feed/?best-topics=top-news&post_type=best",
        "https://rss.dw.com/rdf/rss-en-world",
    ],
    "topics": [],
}


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_CONFIG)


def _write_cache(items: list[dict]) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception as e:  # pragma: no cover
        log.warning("news_cache Schreibfehler: %s", e)


def _read_cache() -> list[dict]:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _source_urls(config: dict) -> list[str]:
    """Erweitert die statischen Feeds um Google-News-Feeds für Themen."""
    urls = list(config.get("feeds", []) or [])
    seen = set(urls)
    for topic in config.get("topics", []) or []:
        if not isinstance(topic, str) or not topic.strip():
            continue
        params = {"q": topic.strip(), "hl": "de", "gl": "DE", "ceid": "DE:de"}
        url = f"{_TOPIC_FEED_URL}?{urlencode(params)}"
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


async def refresh(config: dict | None = None) -> list[dict]:
    """Feeds holen, geolokalisieren, Cache schreiben, Liste zurückgeben."""
    cfg_ = config or _load_config()
    raw = await feeds.fetch_all(_source_urls(cfg_))
    located: list[dict] = []
    for item in raw[:_MAX_ITEMS]:
        try:
            located.append(await geolocate.geolocate_item(item))
        except Exception as e:  # einzelnes Item darf den Lauf nicht killen
            log.debug("Geolokalisierung fehlgeschlagen: %s", e)
            located.append(item)
    _write_cache(located)
    log.info("🌍 News-Globus aktualisiert: %d Meldungen, %d verortet",
             len(located), sum(1 for i in located if "lat" in i))
    return located


def cached() -> list[dict]:
    """Alle zuletzt gecachten Meldungen."""
    return _read_cache()


def cached_geo() -> list[dict]:
    """Nur Meldungen mit Koordinaten (für den Globus)."""
    return [i for i in _read_cache() if "lat" in i and "lon" in i]
