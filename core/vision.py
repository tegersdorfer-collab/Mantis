"""Cloud-Bildanalyse für Foto-, Screen- und andere Bildaufgaben."""
from __future__ import annotations

import base64
import logging

from core.backends.factory import cloud_chain

log = logging.getLogger(__name__)
_chain = None


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
    chain = get_backend_chain()
    content = [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {
            "type": "base64",
            "media_type": "image/jpeg",
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
