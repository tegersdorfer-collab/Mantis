"""Vorgefertigte Audio-Füller für den gestreamten Voice-WebSocket."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import config
from core import tts

log = logging.getLogger(__name__)

FILLERS_DIR = Path(__file__).parent.parent / "data" / "voice_fillers"
FillerKind = Literal["ack", "tool"]


@dataclass(frozen=True, slots=True)
class VoiceFiller:
    kind: FillerKind
    text: str
    audio: bytes
    duration_ms: float | None


_last_used_text: str | None = None


def _tts_identity() -> tuple[str, str]:
    engine = str((tts._engine_chain() or [tts._engine()])[0])
    if engine == "cartesia":
        voice = str(getattr(config, "TTS_CARTESIA_VOICE", ""))
    elif engine == "elevenlabs":
        voice = str(getattr(config, "TTS_ELEVENLABS_VOICE", ""))
    elif engine == "google":
        voice = str(getattr(config, "TTS_GOOGLE_VOICE", ""))
    elif engine == "kokoro" or (
        engine == "local"
        and not tts._kokoro_unavailable
        and all(path.exists() for path in tts._kokoro_paths())
    ):
        voice = str(getattr(config, "TTS_KOKORO_VOICE", ""))
    else:
        try:
            voice = str(tts._active_voice_name())
        except Exception:
            voice = str(tts.DEFAULT_VOICE)
    return engine, voice


def cache_path(
    text: str,
    *,
    engine: str | None = None,
    voice: str | None = None,
) -> Path:
    """Cachepfad aus Engine, Stimme und Füllertext; Änderungen erzeugen neue Dateien."""
    active_engine, active_voice = _tts_identity()
    identity = "\0".join((engine or active_engine, voice or active_voice, text))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return FILLERS_DIR / f"{digest}.ogg"


def _pool(kind: FillerKind) -> list[str]:
    setting = "VOICE_FILLERS_ACK" if kind == "ack" else "VOICE_FILLERS_TOOL"
    values = getattr(config, setting, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (tuple, list)):
        return []
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def _ogg_opus_duration_ms(audio: bytes) -> float | None:
    """Liest die Dauer eines OGG/Opus-Streams aus der letzten Granule-Position."""
    offset = 0
    pre_skip = None
    final_granule = None
    while offset + 27 <= len(audio):
        if audio[offset:offset + 4] != b"OggS":
            return None
        segments = audio[offset + 26]
        table_start = offset + 27
        body_start = table_start + segments
        if body_start > len(audio):
            return None
        body_size = sum(audio[table_start:body_start])
        page_end = body_start + body_size
        if page_end > len(audio):
            return None
        granule = int.from_bytes(audio[offset + 6:offset + 14], "little")
        if granule != 0xFFFFFFFFFFFFFFFF:
            final_granule = granule
        head = audio[body_start:page_end].find(b"OpusHead")
        if head >= 0 and body_start + head + 12 <= page_end:
            pre_skip = int.from_bytes(audio[body_start + head + 10:body_start + head + 12], "little")
        offset = page_end
    if pre_skip is None or final_granule is None:
        return None
    return round(max(0, final_granule - pre_skip) * 1000 / 48_000, 1)


def choose_cached_filler(kind: FillerKind) -> VoiceFiller | None:
    """Wählt einen vorhandenen Cacheeintrag. Diese Funktion erzeugt niemals Audio."""
    global _last_used_text
    candidates: list[tuple[str, bytes]] = []
    for text in _pool(kind):
        path = cache_path(text)
        try:
            audio = path.read_bytes()
        except OSError:
            continue
        if audio:
            candidates.append((text, audio))
    candidates = [item for item in candidates if item[0] != _last_used_text]
    if not candidates:
        return None
    text, audio = random.choice(candidates)
    _last_used_text = text
    return VoiceFiller(kind, text, audio, _ogg_opus_duration_ms(audio))


async def preload_voice_fillers() -> None:
    """Erzeugt fehlende Pooldateien im Hintergrund und lässt Fehler ohne Wirkung."""
    if not getattr(config, "VOICE_FILLERS_ENABLED", True):
        return
    for kind in ("ack", "tool"):
        for text in _pool(kind):
            path = cache_path(text)
            try:
                if path.is_file() and path.stat().st_size:
                    continue
                audio = await tts.synthesize_filler(text)
                if not audio:
                    continue
                # Ein lokaler Hauptprovider kann beim Laden von Kokoro auf Piper
                # umschalten. Den Cache danach unter der tatsächlich aktiven Stimme ablegen.
                path = cache_path(text)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = path.with_name(f"{path.name}.tmp")
                temporary_path.write_bytes(audio)
                temporary_path.replace(path)
            except Exception as exc:
                log.warning(
                    "Voice-Füller konnte nicht vorab erzeugt werden (%s)",
                    type(exc).__name__,
                )


def schedule_voice_fillers_preload(parakeet_task: asyncio.Task | None = None) -> asyncio.Task | None:
    """Startet den Cache-Aufbau nach Abschluss des optionalen Parakeet-Preloads."""
    if not getattr(config, "VOICE_FILLERS_ENABLED", True):
        return None

    async def preload_after_stt() -> None:
        if parakeet_task is not None:
            try:
                await parakeet_task
            except Exception as exc:
                log.warning("Parakeet-Preload vor Voice-Füllern fehlgeschlagen (%s)", type(exc).__name__)
        await preload_voice_fillers()

    return asyncio.create_task(preload_after_stt(), name="voice-fillers-preload")
