"""Nachrichtenpfad für sofortiges, idempotentes Erfassen aus Discord."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from core import db
from domains.inbox import metadata, store, vision
from domains.inbox.capture import (
    classify_item,
    classify_capture,
    extract_source_urls,
    platform_for_url,
    remove_save_prefix,
)

log = logging.getLogger(__name__)
_BERLIN = ZoneInfo("Europe/Berlin")
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_PASSIVE_NOTE = re.compile(
    r"^(?:schau|sehe|lese|will|möchte|moechte|für später|fuer spaeter|später ansehen|spaeter ansehen)\b",
    re.IGNORECASE,
)
_CATEGORY_LABELS = {
    "anschauen": "Anschauen", "lesen": "Lesen", "film-serie": "Film",
    "kaufen": "Kaufen", "idee": "Idee", "ort": "Ort", "zitat": "Zitat",
    "sonstiges": "Sonstiges",
}


async def capture_discord_message(
    message,
    channel,
    *,
    vision_backend,
    embedding_provider=None,
) -> dict:
    """Commited zuerst den Merker, sendet sofort Bestätigung, reichert danach an."""
    started = time.monotonic()
    text = str(getattr(message, "text", "") or "")
    attachments = tuple(getattr(message, "attachments", ()) or ())
    images = _supported_images(attachments)
    decision = classify_capture(text, has_image=bool(images))
    if not decision.capture:
        return {"status": "not_capture", "enrichment_task": None}

    urls = decision.urls or tuple(extract_source_urls(text))
    note = _user_note(text, urls)
    platform = platform_for_url(urls[0]) if urls else "discord"
    modalities = int(bool(urls)) + int(bool(images)) + int(bool(note))
    kind = (
        "mixed" if modalities > 1 else "image" if images else "link" if urls else "text"
    )
    media_kind = "video" if platform in {"youtube", "tiktok", "instagram"} else "article"
    initial_metadata = {"platform": platform, "type": media_kind} if urls else {}
    title = _initial_title(note, urls, images, platform)
    image_description = ""
    vision_available = bool(images) and vision.has_vision_backend(vision_backend)
    if images and not vision_available:
        image_description = note or "Bild aus Discord"
    category, tags = classify_item(note, {**initial_metadata, "image_description": image_description})

    try:
        capture = store.create_item(
            title=title,
            user_note=note,
            source_kind=kind,
            source_urls=list(urls),
            platform=platform,
            source_metadata=initial_metadata,
            image_description=image_description,
            category=category,
            tags=tags,
            source_message_id=getattr(message, "source_message_id", None),
        )
        item = capture["item"]
        if not item:
            raise RuntimeError("Inbox-Merker ohne Datenbankeintrag")
    except Exception as exc:
        _metric("capture", "error", duration_ms=_elapsed_ms(started))
        log.warning("Merk-Inbox-Erfassung fehlgeschlagen (%s)", type(exc).__name__)
        response = "⚠️ Konnte ich gerade nicht merken. Versuch es bitte nochmal."
        await channel.send(response)
        return {"status": "error", "response": response, "enrichment_task": None}

    created = bool(capture.get("created"))
    duplicate_link = bool(capture.get("duplicate_link"))
    status = "created" if created else "duplicate" if duplicate_link else "already_captured"
    response = _confirmation(item, duplicate_link=duplicate_link, already_captured=not created and not duplicate_link)
    if images and not vision_available:
        response += " · Bildanalyse nicht verfügbar"
    attachment_notice = str(getattr(message, "attachment_notice", "") or "").strip()
    if attachment_notice:
        response += f" · {attachment_notice}"
    _metric("capture", status, duration_ms=_elapsed_ms(started))
    try:
        sent_message = await channel.send(response)
    except Exception as exc:
        log.warning("Merk-Inbox-Bestätigung fehlgeschlagen (%s)", type(exc).__name__)
    else:
        confirmation_id = _message_reference_id(sent_message)
        if confirmation_id:
            try:
                store.add_source_message_reference(item["id"], confirmation_id)
            except Exception as exc:
                log.warning("Merk-Inbox-Reply-Referenz fehlgeschlagen (%s)", type(exc).__name__)

    should_enrich = created or duplicate_link
    needs_metadata = created and bool(urls)
    needs_vision = bool(images) and vision_available
    needs_embedding = bool(embedding_provider) and should_enrich
    task = None
    if should_enrich and (needs_metadata or needs_vision or needs_embedding):
        task = asyncio.create_task(_enrich_item(
            item=item,
            urls=list(urls),
            note=note,
            images=images if needs_vision else [],
            vision_backend=vision_backend,
            embedding_provider=embedding_provider,
            needs_metadata=needs_metadata,
            needs_vision=needs_vision,
            needs_embedding=needs_embedding,
            fallback_image_description=image_description or note or "Bild aus Discord",
        ))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {
        "status": status,
        "response": response,
        "item": item,
        "enrichment_task": task,
    }


async def _enrich_item(
    *,
    item: dict,
    urls: list[str],
    note: str,
    images: list[dict],
    vision_backend,
    embedding_provider,
    needs_metadata: bool,
    needs_vision: bool,
    needs_embedding: bool,
    fallback_image_description: str,
) -> None:
    started = time.monotonic()
    async def fetch_metadata():
        if not needs_metadata or not urls:
            return {}
        try:
            return await metadata.fetch_metadata(urls[0], timeout_s=8.0)
        except Exception:
            return {}

    async def describe():
        if not needs_vision:
            return None
        try:
            return await vision.describe_images(
                images, caption=note, backend=vision_backend,
                timeout_s=vision.VISION_TIMEOUT_S,
            )
        except Exception:
            return None

    source_data, description = await asyncio.gather(fetch_metadata(), describe())
    source_data = source_data if isinstance(source_data, dict) else {}
    description = (description or "").strip()
    if images and not description:
        description = fallback_image_description

    existing_metadata = item.get("source_metadata")
    metadata_values = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
    metadata_values.update({
        key: source_data.get(key)
        for key in ("type", "title", "description", "author", "platform", "thumbnail", "published_at")
        if source_data.get(key) not in (None, "")
    })
    effective_note = str(item.get("user_note") or note or "").strip()
    effective_description = description or str(item.get("image_description") or "")
    category, tags = classify_item(
        effective_note,
        {**metadata_values, "image_description": effective_description},
    )
    existing_category = item.get("category")
    if existing_category in {"anschauen", "lesen", "film-serie", "kaufen", "idee", "ort", "zitat"}:
        category = existing_category
    elif category == "sonstiges" and existing_category:
        category = existing_category
    tags = list(dict.fromkeys([*(item.get("tags") or []), *tags]))
    item_platform = str(item.get("platform") or (platform_for_url(urls[0]) if urls else "unknown"))
    fallback_title = _initial_title("", urls, images, item_platform)
    current_title = str(item.get("title") or "").strip()
    has_user_title = bool(effective_note) and current_title.casefold() != fallback_title.casefold()
    can_set_external_title = not has_user_title and current_title.casefold() == fallback_title.casefold()
    enriched_title = source_data.get("title") if can_set_external_title else None
    title_for_embedding = str(
        enriched_title or item.get("title") or metadata_values.get("title") or "Merker"
    )

    embedding = None
    if needs_embedding and embedding_provider is not None:
        try:
            vector = await asyncio.wait_for(
            embedding_provider.embed(
                f"{title_for_embedding}\n{effective_note}\n{effective_description}".strip()
            ), timeout=3.0
            )
            if vector:
                embedding = list(vector)
        except Exception:
            embedding = None

    try:
        store.update_enrichment(
            item["id"],
            title=enriched_title,
            title_if_current=fallback_title if enriched_title else None,
            platform=source_data.get("platform"),
            source_metadata=metadata_values if source_data else None,
            image_description=description if images else None,
            category=category,
            tags=tags,
            embedding=embedding,
        )
        _metric(
            "enrichment",
            "updated" if source_data or description or embedding else "fallback",
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        _metric("enrichment", "error", duration_ms=_elapsed_ms(started))
        log.warning("Merk-Inbox-Anreicherung fehlgeschlagen (%s)", type(exc).__name__)
    finally:
        # Discord-Bildbytes leben nur bis zum Ende dieses Tasks.
        images.clear()


def _supported_images(attachments) -> list[dict]:
    supported = []
    for attachment in attachments:
        if isinstance(attachment, dict):
            image = attachment
        else:
            image = {
                "filename": getattr(attachment, "filename", ""),
                "content_type": getattr(attachment, "content_type", ""),
                "size": getattr(attachment, "size", None),
                "data": getattr(attachment, "data", None),
            }
        content_type = str(image.get("content_type") or "").split(";", 1)[0].casefold()
        data = image.get("data")
        size = image.get("size")
        if content_type not in vision.SUPPORTED_IMAGE_TYPES:
            continue
        if not isinstance(data, (bytes, bytearray)) or not data or len(data) > vision.MAX_IMAGE_BYTES:
            continue
        if size is not None and int(size) > vision.MAX_IMAGE_BYTES:
            continue
        supported.append({"data": bytes(data), "content_type": content_type})
        if len(supported) == vision.MAX_IMAGES:
            break
    return supported


def _user_note(text: str, urls) -> str:
    value = text or ""
    for url in urls:
        value = value.replace(url, " ")
    value = re.sub(r'https?://[^\s<>"\']+', " ", value)
    value = remove_save_prefix(value)
    return " ".join(value.split()).strip(" .,;:-")[:12000]


def _message_reference_id(sent_message) -> str | None:
    if isinstance(sent_message, (str, int)):
        return str(sent_message)
    value = getattr(sent_message, "id", None)
    return str(value) if value is not None else None


def _initial_title(note: str, urls, images, platform: str) -> str:
    descriptive = note.strip()
    if descriptive and not _PASSIVE_NOTE.search(descriptive):
        return descriptive.splitlines()[0][:100].strip(" .,:;-") or "Merker"
    if urls:
        if platform == "youtube":
            return "YouTube-Link"
        if platform == "tiktok":
            return "TikTok-Link"
        if platform == "instagram":
            return "Instagram-Link"
        host = (urlparse(urls[0]).hostname or "Link").removeprefix("www.")
        return f"Link · {host}"[:100]
    if images:
        return "Bild aus Discord"
    return descriptive[:100] or "Merker"


def _confirmation(item: dict, *, duplicate_link: bool, already_captured: bool) -> str:
    item_id = item.get("id", "?")
    if duplicate_link:
        created_at = item.get("created_at")
        if created_at is None:
            date = "vorher"
        else:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=_BERLIN)
            date = created_at.astimezone(_BERLIN).strftime("%d.%m.")
        return f"📌 Hast du dir schon am {date} gemerkt · ID {item_id}"
    if already_captured:
        return f"📌 Schon erfasst · ID {item_id}"
    title = str(item.get("title") or "Merker").replace("\n", " ")[:100]
    category = _CATEGORY_LABELS.get(item.get("category"), "Sonstiges")
    tags = " ".join(f"#{tag}" for tag in (item.get("tags") or [])[:2])
    suffix = f" · {tags}" if tags else ""
    return f"📥 gemerkt: „{title}“ · {category}{suffix} · ID {item_id}"


def _metric(operation: str, status: str, *, duration_ms: int | None = None) -> None:
    detail = {"status": status}
    if duration_ms is not None:
        detail["duration_ms"] = max(0, int(duration_ms))
    db.log_event("inbox", operation, detail)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
