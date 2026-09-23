"""
Vision — gemeinsame Ollama-Bildbeschreibung.

Identisch zu dem, was communication/telegram.py bisher exklusiv für
Foto-Nachrichten nutzte — jetzt hier zentralisiert, damit weitere Konsumenten
(z.B. Screen-Context-Awareness in core/skills/vision.py) dieselbe Logik
wiederverwenden statt sie zu duplizieren (analog zu core/voice.py für Whisper).
"""
import base64
import logging

import config

log = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = getattr(config, "VISION_MODEL", "qwen3.5:9b")


async def describe_image(image_bytes: bytes, prompt: str, model: str | None = None) -> str:
    """Beschreibt ein Bild lokal via Ollama-Vision-Modell. Gibt einen Fallback-Text
    bei Fehlern zurück, statt eine Exception zu werfen."""
    try:
        import ollama as _ollama
        b64 = base64.standard_b64encode(image_bytes).decode()
        client = _ollama.AsyncClient(host=config.OLLAMA_BASE_URL)
        resp = await client.chat(
            model=model or DEFAULT_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": prompt,
                "images": [b64],
            }],
            options={"num_predict": 512},
            think=False,  # qwen3.5 denkt sonst und verbraucht das Budget im Thinking
            keep_alive=0,
        )
        return (resp.message.content or "").strip()
    except Exception as e:
        log.error(f"Ollama-Vision fehlgeschlagen: {e}")
        return "Konnte Bild nicht analysieren."
