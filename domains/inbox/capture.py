"""Regelbasierte Erkennung und Metadaten-Klassifikation für Inbox-Merker."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from tools.url_handler import extract_urls as _extract_urls


_SAVE_START = re.compile(
    r"^\s*(?:"
    r"merk\s+dir|merke(?:\s+dir)?|für\s+später|fuer\s+später|"
    r"für\s+spaeter|fuer\s+spaeter|später\s+(?:anschauen|ansehen|lesen)|"
    r"spaeter\s+(?:anschauen|ansehen|lesen)|inbox\s*:|📥|"
    r"speicher(?:e)?|notier(?:e)?|behalte\s+das|halte\s+das\s+fest|"
    r"(?:pack\s+das\s+)?auf\s+meine\s+merkliste|"
    r"später\s+kaufen|spaeter\s+kaufen|save\s+this|remember\s+this|"
    r"for\s+later|save\s+for\s+later"
    r")(?=\b|$|\s|:)",
    re.IGNORECASE,
)
_WORK_REQUEST = re.compile(
    r"\b(?:vergleiche?|erklär(?:e)?|erklaer(?:e)?|unmerk(?:e)?|fass(?:e)?\s+(?:das\s+)?zusammen|"
    r"zusammenfassen|prüf(?:e)?|pruef(?:e)?|übersetz(?:e)?|uebersetz(?:e)?|"
    r"such(?:e)?\s+(?:mir|nach)|finde\s+(?:heraus|mir)|öffne|oeffne|"
    r"analysier(?:e)?|bewerte|prüfen|pruefen|fix(?:e|en)?|"
    r"lösch(?:e)?|loesch(?:e)?|entfern(?:e)?|korrigier(?:e)?|aender(?:e)?|änder(?:e)?|"
    r"schau\s+(?:dir|mal|das|den|die|diesen|diese)|sieh\s+dir|lies\b|lese\s+(?:dir|den|die|das)|"
    r"check(?:e)?\b|read\b|look\s+at|review\b|recommend\b|"
    r"zeig(?:e)?|zeige|kannst\s+du|sag\s+mir|was|wie|warum|wieso|wer|wo|"
    r"welche|welcher|welches|ist\s+das|is\s+this|what|how|why|compare|explain|"
    r"summari[sz]e)\b",
    re.IGNORECASE,
)
_CHATTER_OR_ASSERTION = re.compile(
    r"\b(?:ich\s+(?:habe|hab|bin|war|glaube|denke|finde)|"
    r"mein(?:e|er|en)?\s+\w+\s+(?:ist|sind|war|wird)|das\s+ist\s+mein)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:nicht|nie|niemals)\b", re.IGNORECASE)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "si"}
_TAG_PATTERNS = (
    ("scifi", re.compile(r"\b(?:sci[ -]?fi|science fiction)\b", re.I)),
    ("balkon", re.compile(r"\bbalkon\w*\b", re.I)),
    ("kochen", re.compile(r"\b(?:kochen|rezept|recipe|cooking)\b", re.I)),
    ("reisen", re.compile(r"\b(?:reise|reisen|urlaub|travel|reiseziel)\b", re.I)),
    ("design", re.compile(r"\bdesign\w*\b", re.I)),
    ("python", re.compile(r"\bpython\b", re.I)),
    ("ki", re.compile(r"\b(?:ki|künstliche intelligenz|kuenstliche intelligenz|ai)\b", re.I)),
)


@dataclass(frozen=True)
class CaptureDecision:
    capture: bool
    reason: str
    urls: tuple[str, ...] = ()


def extract_source_urls(text: str) -> list[str]:
    """Findet HTTP(S)-Links und entfernt übliche Satzzeichen am Ende."""
    urls = []
    seen = set()
    for candidate in _extract_urls(text or ""):
        clean = candidate.rstrip(".,;:!?)]}>")
        parsed = urlparse(clean)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        key = normalize_url(clean)
        if key and key not in seen:
            urls.append(clean)
            seen.add(key)
    return urls


def classify_capture(text: str, *, has_image: bool = False) -> CaptureDecision:
    """Entscheidet konservativ, ob eine Discord-Nachricht sofort gemerkt wird."""
    clean_text = (text or "").strip()
    urls = extract_source_urls(clean_text)
    outside_urls = _outside_url_text(clean_text, urls).strip()
    explicit_match = _SAVE_START.match(outside_urls)
    if explicit_match and _NEGATION.search(outside_urls[explicit_match.end():]):
        return CaptureDecision(False, "negated_save", tuple(urls))
    asks_to_summarize = re.search(r"\bfass(?:e)?\b.{0,40}\bzusammen\b", outside_urls, re.I)
    if "?" in outside_urls or _WORK_REQUEST.search(outside_urls) or asks_to_summarize:
        return CaptureDecision(False, "agent_request", tuple(urls))

    if explicit_match:
        return CaptureDecision(True, "explicit_save", tuple(urls))

    if has_image:
        if not outside_urls:
            return CaptureDecision(True, "image_share", tuple(urls))
        return CaptureDecision(False, "image_with_caption", tuple(urls))

    if urls:
        if not outside_urls:
            return CaptureDecision(True, "bare_link", tuple(urls))
        word_count = len(re.findall(r"\b[\wÀ-ž]+\b", outside_urls, flags=re.UNICODE))
        if word_count <= 12 and not _CHATTER_OR_ASSERTION.search(outside_urls):
            return CaptureDecision(True, "short_share_note", tuple(urls))
    return CaptureDecision(False, "normal_chat", tuple(urls))


def _outside_url_text(text: str, urls: list[str]) -> str:
    """Entfernt Links vor Frage- und Auftragsprüfungen, auch bei URL-Querystrings."""
    outside = text
    for url in urls:
        outside = outside.replace(url, " ")
    return re.sub(r"https?://[^\s<>\"']+", " ", outside, flags=re.IGNORECASE)


def remove_save_prefix(text: str) -> str:
    """Entfernt einen führenden Merk-Auslöser aus dem gespeicherten Nutztext."""
    value = str(text or "")
    match = _SAVE_START.match(value)
    if not match:
        return value
    value = value[match.end():]
    if match.group().strip() == "📥":
        second_match = _SAVE_START.match(value)
        if second_match:
            value = value[second_match.end():]
    value = re.sub(
        r"^\s*(?:(?:das|diese idee|diesen link|bitte)\b\s*)?[:,-]?\s*",
        "", value, flags=re.IGNORECASE,
    )
    return value


def normalize_url(url: str) -> str:
    """Kanonisiert Links für die offene-Link-Deduplizierung."""
    try:
        parsed = urlparse((url or "").strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
            return ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        port = parsed.port
        netloc = hostname
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{hostname}:{port}"
        path = parsed.path.rstrip("/") or "/"
        query = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_KEYS and not key.casefold().startswith("utm_")
        ]
        query.sort()
        return urlunparse((scheme, netloc, path, "", urlencode(query, doseq=True), ""))
    except (TypeError, ValueError):
        return ""


def platform_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    return "article"


def classify_item(text: str, metadata: dict | None = None) -> tuple[str, list[str]]:
    """Ordnet Inhalte einer v1-Kategorie zu und erzeugt thematische Tags."""
    metadata = metadata or {}
    content = " ".join(str(part) for part in (
        text, metadata.get("title", ""), metadata.get("description", ""),
        metadata.get("image_description", ""),
    ) if part).casefold()
    platform = str(metadata.get("platform") or "").casefold()
    kind = str(metadata.get("type") or "").casefold()

    if re.search(r"\b(zitat|quote|spruch)\b", content):
        category = "zitat"
    elif re.search(r"\b(kaufen|bestellen|shop|angebot|produkt|wunschliste)\b", content):
        category = "kaufen"
    elif re.search(r"\b(restaurant|café|cafe|hotel|ort|reiseziel|location)\b", content):
        category = "ort"
    elif re.search(r"\b(film|serie|staffel|kino|schauspieler)\b", content):
        category = "film-serie"
    elif re.search(r"\b(idee|inspiration|balkon|entwurf|konzept)\b", content):
        category = "idee"
    elif platform in {"youtube", "tiktok", "instagram"} or kind == "video":
        category = "anschauen"
    elif kind == "article" or re.search(r"\b(artikel|blog|buch|lesen|essay)\b", content):
        category = "lesen"
    else:
        category = "sonstiges"

    tags = []
    for tag, pattern in _TAG_PATTERNS:
        if pattern.search(content):
            tags.append(tag)
    for raw_tag in re.findall(r"(?<!\w)#([\w-]{2,32})", text or ""):
        tag = normalize_tag(raw_tag)
        if tag and tag not in tags:
            tags.append(tag)
    return category, tags[:8]


def normalize_tag(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lstrip("#"))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-zA-Z0-9]+", "", normalized.casefold())
    return normalized[:32]
