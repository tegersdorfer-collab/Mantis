"""Prozessweite Laufzeitschätzung für STT-Aufrufe im Voice-Pfad."""
from __future__ import annotations

from collections import deque
import math
import threading

INITIAL_MS_PER_AUDIO_SECOND = 400.0
FIXED_OVERHEAD_MS = 150.0
_SAMPLE_WINDOW = 10
_samples: deque[float] = deque(maxlen=_SAMPLE_WINDOW)
_samples_lock = threading.Lock()


def record_stt_runtime(audio_seconds: float, elapsed_ms: float) -> None:
    """Speichert eine STT-Laufzeit als Millisekunden pro Audiosekunde."""
    try:
        audio_seconds = float(audio_seconds)
        elapsed_ms = float(elapsed_ms)
    except (TypeError, ValueError):
        return
    if (
        not math.isfinite(audio_seconds)
        or not math.isfinite(elapsed_ms)
        or audio_seconds <= 0
        or elapsed_ms < 0
    ):
        return
    with _samples_lock:
        _samples.append(elapsed_ms / audio_seconds)


def estimate_stt_runtime_ms(audio_seconds: float) -> float:
    """Schätzt STT-Laufzeit mit dem gleitenden Mittelwert oder einem Startwert."""
    try:
        audio_seconds = max(0.0, float(audio_seconds))
    except (TypeError, ValueError):
        audio_seconds = 0.0
    if not math.isfinite(audio_seconds):
        audio_seconds = 0.0
    with _samples_lock:
        rate = sum(_samples) / len(_samples) if _samples else INITIAL_MS_PER_AUDIO_SECOND
    return rate * audio_seconds + FIXED_OVERHEAD_MS
