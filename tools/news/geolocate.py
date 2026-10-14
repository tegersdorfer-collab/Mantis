"""News-Geolokalisierung — Schlagzeile → Ort → Koordinaten.

Der Ort wird über die schnelle Cloud-Kette aus der Schlagzeile extrahiert
(robuster als Regex/Gazetteer) und dann über Nominatim geocodiert. Ergebnis pro
Schlagzeile gecacht (kein doppelter LLM-Call). Test-Seams: _extract_place (LLM)
und nominatim.geocode.
"""
from __future__ import annotations

import json
import logging
import os

from core import fast
from tools.geo import nominatim

log = logging.getLogger("core.news")

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "news_place_cache.json")

_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache or {}, f, ensure_ascii=False)
    except Exception as e:  # pragma: no cover
        log.warning("news_place_cache Schreibfehler: %s", e)


async def _extract_place(text: str) -> str:  # pragma: no cover - live-only
    """Hauptort einer Schlagzeile (leerer String, wenn keiner erkennbar)."""
    prompt = (
        "Nenne den EINEN geografischen Hauptort (Stadt oder Land) dieser Schlagzeile "
        "als ganz kurze Antwort, ohne Zusatz. Gibt es keinen klaren Ort, antworte NONE.\n\n"
        f"Schlagzeile: {text}"
    )
    out = await fast.ask(prompt, max_tokens=12)
    out = (out or "").strip().strip('."„“ ')
    return "" if not out or out.upper() == "NONE" else out


async def locate_headline(title: str, summary: str = "") -> str | None:
    """Ortsname der Schlagzeile (gecacht) oder None."""
    key = (title or "").strip().lower()
    if not key:
        return None
    cache = _load_cache()
    if key in cache:
        return cache[key]
    place = await _extract_place(f"{title}. {summary}".strip())
    result = place or None
    cache[key] = result
    _save_cache()
    return result


async def geolocate_item(item: dict) -> dict:
    """Item-Kopie mit {place, lat, lon}, falls ein Ort erkannt + geocodiert wurde."""
    out = dict(item)
    place = await locate_headline(item.get("title", ""), item.get("summary", ""))
    if place:
        geo = await nominatim.geocode(place)
        if geo:
            out["place"] = place
            out["lat"] = geo["lat"]
            out["lon"] = geo["lon"]
    return out
