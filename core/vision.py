"""Cloud-Bildanalyse für Foto-, Screen- und andere Bildaufgaben."""
from __future__ import annotations

import base64
import logging

from core.backends.factory import cloud_chain

log = logging.getLogger(__name__)
_chain = None
MAX_IMAGE_BASE64_BYTES = 10_000_000
MAX_IMAGE_RAW_BYTES = (MAX_IMAGE_BASE64_BYTES // 4) * 3
MAX_IMAGE_DIMENSION = 8_000


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            return None
        if marker in start_of_frame and segment_length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _image_info(image_bytes: bytes) -> tuple[str, int, int]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        if image_bytes[12:16] != b"IHDR":
            raise ValueError("Ungültiger PNG-Header")
        width, height = int.from_bytes(image_bytes[16:20], "big"), int.from_bytes(image_bytes[20:24], "big")
        media_type = "image/png"
    elif image_bytes.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(image_bytes)
        if dimensions is None:
            raise ValueError("Ungültiger JPEG-Header")
        width, height = dimensions
        media_type = "image/jpeg"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")) and len(image_bytes) >= 10:
        width = int.from_bytes(image_bytes[6:8], "little")
        height = int.from_bytes(image_bytes[8:10], "little")
        media_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP" and len(image_bytes) >= 25:
        chunk = image_bytes[12:16]
        if chunk == b"VP8X" and len(image_bytes) >= 30:
            width = int.from_bytes(image_bytes[24:27], "little") + 1
            height = int.from_bytes(image_bytes[27:30], "little") + 1
        elif chunk == b"VP8 " and len(image_bytes) >= 30 and image_bytes[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(image_bytes[26:28], "little") & 0x3FFF
            height = int.from_bytes(image_bytes[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L" and len(image_bytes) >= 25 and image_bytes[20] == 0x2F:
            b1, b2, b3, b4 = image_bytes[21:25]
            width = (b1 | ((b2 & 0x3F) << 8)) + 1
            height = ((b2 >> 6) | (b3 << 2) | ((b4 & 0x0F) << 10)) + 1
        else:
            raise ValueError("Ungültiger oder nicht unterstützter WebP-Header")
        media_type = "image/webp"
    else:
        raise ValueError("Bildformat wird nicht unterstützt (JPEG, PNG, GIF oder WebP erwartet)")

    if width <= 0 or height <= 0:
        raise ValueError("Bildabmessungen sind ungültig")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError("Bild überschreitet die maximalen Anthropic-Abmessungen von 8000x8000 Pixeln")
    return media_type, width, height


def validate_image_payload(image_bytes: bytes) -> str:
    """Prüft das Anbieterlimit und liefert den anhand der Bytes erkannten MIME-Typ."""
    encoded_size = 4 * ((len(image_bytes) + 2) // 3)
    if encoded_size > MAX_IMAGE_BASE64_BYTES:
        raise ValueError("Bild überschreitet das Anthropic-Limit von 10 MB nach Base64-Kodierung")
    media_type, _width, _height = _image_info(image_bytes)
    return media_type


def get_backend_chain():
    global _chain
    if _chain is None:
        _chain = cloud_chain("vision", reasoning="low")
    return _chain


async def ask_image(
    image_bytes: bytes,
    prompt: str,
    *,
    max_tokens: int = 512,
    temperature: float = 0.2,
    format: str | dict | None = None,
) -> str:
    """Sendet ein Bild über die Vision-Variante der zentralen Cloud-Kette."""
    media_type = validate_image_payload(image_bytes)
    chain = get_backend_chain()
    content = [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }},
    ]
    messages = []
    if format == "json":
        messages.append({
            "role": "system",
            "content": "Antworte ausschließlich mit validem JSON, ohne Text drumherum.",
        })
    elif isinstance(format, dict):
        messages.append({
            "role": "system",
            "content": "Antworte als JSON gemäß diesem Schema: " + str(format),
        })
    messages.append({"role": "user", "content": content})
    answer, _ = await chain.call(
        messages=messages,
        tools=None,
        stream_cb=None,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (answer or "").strip()


async def describe_image(image_bytes: bytes, prompt: str) -> str:
    """Beschreibt ein Bild; Fehler bleiben für optionale UI-Kontexte lesbar."""
    try:
        return await ask_image(image_bytes, prompt)
    except Exception as exc:
        log.error("Cloud-Vision fehlgeschlagen: %s", exc)
        return "Konnte Bild nicht analysieren."
