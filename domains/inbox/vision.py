"""Cloud-Vision für Inbox-Bilder über bildfähige Stufen der Chat-Kette."""

from __future__ import annotations

import asyncio
import base64


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
VISION_TIMEOUT_S = 8.0


def _stages(backend):
    backends = getattr(backend, "backends", None)
    return tuple(backends) if isinstance(backends, (tuple, list)) else (backend,)


def has_vision_backend(backend) -> bool:
    return any(bool(getattr(stage, "supports_vision", False)) for stage in _stages(backend))


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
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": content_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
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
