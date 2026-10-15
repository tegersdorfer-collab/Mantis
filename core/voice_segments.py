"""Optionale lokale Mitschnitte finaler Voice-Segmente."""
from __future__ import annotations

import io
import json
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path

import config

SAMPLE_RATE = 16_000
_RECORD_LOCK = threading.Lock()


def record_voice_segment(
    pcm_audio: bytes,
    metadata: dict,
    *,
    root_dir: str | Path | None = None,
    now: datetime | None = None,
    max_files: int | None = None,
) -> tuple[Path, Path]:
    """Schreibt ein PCM-Segment als WAV/JSON und begrenzt die Zahl der Paare."""
    audio = bytes(pcm_audio)
    if len(audio) % 2:
        raise ValueError("Voice-Segment muss vollständiges 16-Bit-PCM enthalten")
    timestamp = now or datetime.now().astimezone()
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    root = Path(root_dir) if root_dir is not None else (
        Path(__file__).resolve().parents[1] / "data" / "voice_segments"
    )
    daily_dir = root / timestamp.strftime("%Y-%m-%d")
    stem = f"{timestamp.strftime('%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"
    wav_path = daily_dir / f"{stem}.wav"
    json_path = daily_dir / f"{stem}.json"

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio)

    payload = {
        **dict(metadata or {}),
        "timestamp": timestamp.isoformat(),
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_width_bytes": 2,
    }
    limit = int(
        getattr(config, "VOICE_RECORD_MAX_FILES", 300)
        if max_files is None else max_files
    )
    if limit < 2:
        raise ValueError("VOICE_RECORD_MAX_FILES muss WAV- und JSON-Datei erlauben")
    with _RECORD_LOCK:
        daily_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(wav_path, wav_buffer.getvalue())
        _write_atomic(
            json_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _trim_recordings(root, limit)
    return wav_path, json_path


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _trim_recordings(root: Path, maximum_files: int) -> None:
    manifests = sorted(root.rglob("*.json"))
    maximum_pairs = maximum_files // 2
    for old_manifest in manifests[:-maximum_pairs]:
        old_manifest.with_suffix(".wav").unlink(missing_ok=True)
        old_manifest.unlink(missing_ok=True)
