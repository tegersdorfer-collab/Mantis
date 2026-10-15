"""openWakeWord-Wrapper — erkennt das Wake-Word 'Mantis' im laufenden Audiostrom,
ersetzt für die Erstaktivierung den LLM-basierten is_addressed_to_mantis()-Check
aus core/voice.py (siehe docs/superpowers/specs/2026-07-05-vad-wakeword-streaming-design.md).

WICHTIG: nutzt NICHT openwakeword.model.Model.predict()/predict_clip() — deren
interne Streaming-/Puffer-Logik passt nicht zu dem festen 16-Frame-Fenster, auf
dem unser Custom-Klassifikator-Kopf (scripts/wakeword/train_wakeword.py) trainiert
wurde (predict() lieferte beim Validierungsskript durchgehend 0.0, predict_clip()
uneindeutige Scores — siehe docs/superpowers/plans/2026-07-05-wakeword-training.md,
Task 5). Stattdessen wird derselbe manuelle Embedding+Sliding-Window-Pfad wie
Training/Validierung/Live-Hörtest nachgebaut."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

MODEL_KEY = "mantis"

TARGET_SR = 16000
N_FRAMES = 16           # Fenstergröße, auf die der Klassifikator-Kopf trainiert wurde
EMBEDDING_DIM = 96

# Der Klassifikator braucht ~2s Kontext für ein volles 16-Frame-Embedding-Fenster
# (siehe scripts/wakeword/train_wakeword.py's MIN_CLIP_SAMPLES) — ein einzelner
# 80ms-Chunk allein reicht bei weitem nicht. WakeWordDetector hält daher einen
# rollierenden Puffer über mehrere check()-Aufrufe hinweg, genau wie
# scripts/wakeword/listen_live.py beim Live-Hörtest.
BUFFER_SECONDS = 2.0
BUFFER_SAMPLES = int(BUFFER_SECONDS * TARGET_SR)

# openWakeWord's documented standard chunk size ist 1280 samples (80ms @
# 16kHz) — das ist die Chunk-Größe, auf die core/voice_stream.py::VoiceStreamSession
# den eingehenden Rohstrom vorpuffert, bevor check() aufgerufen wird.
WAKEWORD_REQUIRED_SAMPLES = 1280

# Ein Live-Streaming-Test (kontinuierliches Audio statt isolierter 2s-Testclips,
# siehe docs/superpowers/plans/2026-07-05-vad-wakeword-streaming.md Task 6) zeigte
# kurze Fehlauslöser an Puffer-Übergängen (wenn sich der Fensterinhalt zwischen
# zwei Aufrufen stark ändert) — die reine isolierte Offline-Validierung
# (validate_wakeword.py) bildet das nicht ab. Standard-Fix bei Wake-Word-Systemen:
# erst nach mehreren AUFEINANDERFOLGENDEN Treffern über der Schwelle auslösen
# (Hysterese/Debounce), statt auf einen einzelnen Chunk zu vertrauen.
CONSECUTIVE_HITS_REQUIRED = 3


def _ensure_preprocessor_models_downloaded() -> None:
    """openWakeWords Melspectrogram-/Embedding-Modelle sind nicht im PyPI-Paket
    enthalten (nur bei explizitem download_models()-Aufruf) — ohne sie wirft
    AudioFeatures() einen NoSuchFile-Fehler beim ersten Start.

    download_models() lädt Melspectrogram-/Embedding-/VAD-Modelle IMMER nach
    (idempotent, prüft selbst ob schon vorhanden), unabhängig vom model_names-
    Argument — nur wenn model_names LEER ist, lädt es zusätzlich alle offiziellen
    Wake-Word-Modelle mit herunter (unnötig groß für unseren Custom-Mantis-Case).
    Ein nicht-existenter Platzhalter-Name unterdrückt genau diesen Teil, ohne die
    Melspectrogram-/Embedding-/VAD-Downloads zu verhindern."""
    from openwakeword.utils import download_models
    download_models(["__no_official_wakeword_models_needed__"])


def _load_audio_features():
    _ensure_preprocessor_models_downloaded()
    from openwakeword.utils import AudioFeatures
    return AudioFeatures(inference_framework="onnx", device="cpu")


def _load_session(model_path: Path):
    import onnxruntime as ort
    return ort.InferenceSession(str(model_path))


class WakeWordDetector:
    def __init__(self, model_path: Path, threshold: float = 0.5):
        self._audio_features = _load_audio_features()
        self._session = _load_session(model_path)
        self._input_name = self._session.get_inputs()[0].name
        self._threshold = threshold
        self._buffer = np.zeros(BUFFER_SAMPLES, dtype=np.int16)
        self._consecutive_hits = 0
        self._samples_processed = 0
        self._first_positive_frame_end_sample: int | None = None

    @property
    def first_positive_frame_end_sample(self) -> int | None:
        """Ende des ersten über der Schwelle liegenden Frames der Treffer-Serie."""
        return self._first_positive_frame_end_sample

    def check(self, pcm_chunk: bytes) -> bool:
        n_samples = len(pcm_chunk) // 2
        if n_samples != WAKEWORD_REQUIRED_SAMPLES:
            raise ValueError(
                f"WakeWordDetector.check requires exactly "
                f"{WAKEWORD_REQUIRED_SAMPLES} samples ({WAKEWORD_REQUIRED_SAMPLES * 2} bytes), "
                f"got {n_samples} samples ({len(pcm_chunk)} bytes). Callers must "
                f"buffer raw audio to this frame size before calling (see "
                f"core/voice_stream.py::VoiceStreamSession)."
            )
        chunk = np.array(struct.unpack(f"<{n_samples}h", pcm_chunk), dtype=np.int16)
        self._samples_processed += n_samples
        self._buffer = np.concatenate([self._buffer, chunk])[-BUFFER_SAMPLES:]

        embeddings = self._audio_features._get_embeddings(self._buffer)
        if embeddings.shape[0] < N_FRAMES:
            self._consecutive_hits = 0
            self._first_positive_frame_end_sample = None
            return False

        scores = []
        for i in range(0, embeddings.shape[0] - N_FRAMES + 1):
            window = embeddings[i:i + N_FRAMES][np.newaxis, :, :].astype(np.float32)
            out = self._session.run(None, {self._input_name: window})
            scores.append(float(np.array(out[0]).flatten()[0]))
        score = max(scores) if scores else 0.0

        if score >= self._threshold:
            if self._consecutive_hits == 0:
                self._first_positive_frame_end_sample = self._samples_processed
            self._consecutive_hits += 1
        else:
            self._consecutive_hits = 0
            self._first_positive_frame_end_sample = None

        return self._consecutive_hits >= CONSECUTIVE_HITS_REQUIRED
