"""Cloud-Vision für Inbox-Bilder über bildfähige Stufen der Chat-Kette."""

from __future__ import annotations

import asyncio
import base64
import json

from core.vision import MAX_IMAGE_RAW_BYTES, validate_image_payload


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGES = 4
# 7.5 MB rohe Bytes ergeben höchstens 10 MB nach Base64-Kodierung.
MAX_IMAGE_BYTES = MAX_IMAGE_RAW_BYTES
# Anthropic begrenzt den ganzen Request auf 32 MB. 2 MB bleiben für Modell,
# Systemtext und weitere Felder der jeweiligen Backend-Payload reserviert.
MAX_REQUEST_PAYLOAD_BYTES = 30_000_000
VISION_TIMEOUT_S = 8.0


def _stages(backend):
    backends = getattr(backend, "backends", None)
    return tuple(backends) if isinstance(backends, (tuple, list)) else (backend,)


def has_vision_backend(backend) -> bool:
    return any(bool(getattr(stage, "supports_vision", False)) for stage in _stages(backend))


def _request_payload_size(content: list[dict]) -> int:
    payload = {
        "messages": [{"role": "user", "content": content}],
        "tools": None,
        "temperature": 0.1,
        "max_tokens": 500,
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def describe_images(
    images: list[dict],
    *,
    caption: str,
    backend,
    timeout_s: float = VISION_TIMEOUT_S,
) -> str | None:
    """Beschreibt bis zu vier geprüfte Bilder, ohne Chatverlauf oder lokalen Fallback."""
    content = [{
        "type": "text",
        "text": (
            "Analysiere nur die beigefügten Bilder und den Begleittext. Lies sichtbare Texte "
            "wörtlich (OCR), nenne erkennbare Titel, Marken und Orte, und ergänze eine kurze "
            "neutrale Beschreibung für spätere Suche. Trenne sichtbare Fakten von Vermutungen; "
            "antworte knapp auf Deutsch.\nBegleittext: " + ((caption or "").strip() or "(keiner)")
        ),
    }]
    for image in (images or [])[:MAX_IMAGES]:
        data = image.get("data")
        content_type = str(image.get("content_type") or "").split(";", 1)[0].casefold()
        if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_IMAGE_BYTES:
            continue
        if content_type not in SUPPORTED_IMAGE_TYPES:
            continue
        try:
            content_type = validate_image_payload(bytes(data))
        except ValueError:
            continue
        if content_type not in SUPPORTED_IMAGE_TYPES:
            continue
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": content_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
        if _request_payload_size([*content, image_block]) > MAX_REQUEST_PAYLOAD_BYTES:
            continue
        content.append(image_block)
    if len(content) == 1:
        return None

    stages = _stages(backend)
    stage_timeouts = getattr(backend, "stage_timeouts", ())
    if not isinstance(stage_timeouts, (tuple, list)):
        stage_timeouts = ()
    for index, stage in enumerate(stages):
        if not bool(getattr(stage, "supports_vision", False)):
            continue
        stage_timeout = stage_timeouts[index] if index < len(stage_timeouts) else None
        timeout = min(timeout_s, stage_timeout) if stage_timeout else timeout_s
        try:
            result = await asyncio.wait_for(
                stage.call(
                    messages=[{"role": "user", "content": content}],
                    tools=None,
                    stream_cb=None,
                    temperature=0.1,
                    max_tokens=500,
                ),
                timeout=timeout,
            )
            text = (result[0] or "").strip()
            if text:
                return text[:12000]
        except Exception:
            # Bildanalyse ist best effort. Fehlertexte/URL-Parameter bleiben aus Logs.
            continue
    return None
