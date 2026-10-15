"""Lokale Smart-Turn-v3-Inferenz auf Whisper-Log-Mel-Features."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import config

SAMPLE_RATE = 16_000
WINDOW_SECONDS = 8
WINDOW_SAMPLES = SAMPLE_RATE * WINDOW_SECONDS
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmartTurnPrediction:
    probability: float
    elapsed_ms: float


class SmartTurnAnalyzer:
    """Lädt Feature-Extractor und ONNX-Modell einmal in einem Worker-Thread."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        session_factory=None,
        feature_extractor_factory=None,
    ) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "turn" / "smart-turn-v3.2-cpu.onnx"
        configured_path = model_path or getattr(config, "VOICE_SMART_TURN_MODEL", default_path)
        self.model_path = Path(configured_path).expanduser()
        self._session_factory = session_factory
        self._feature_extractor_factory = feature_extractor_factory
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smart-turn",
        )
        self._runtime_lock = threading.Lock()
        self._session = None
        self._feature_extractor = None
        self._input_name: str | None = None
        self._output_name: str | None = None
        self._closed = False
        self._load_error: Exception | None = None

    async def predict(self, pcm_audio: bytes) -> SmartTurnPrediction:
        """Führt die Vorhersage außerhalb des Event-Loops aus."""
        if self._closed:
            raise RuntimeError("SmartTurnAnalyzer ist geschlossen")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._predict_sync, bytes(pcm_audio),
        )

    async def warmup(self) -> None:
        """Lädt das Modell im Worker vor dem ersten stillen Turn-Check."""
        if self._closed:
            raise RuntimeError("SmartTurnAnalyzer ist geschlossen")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._get_runtime)

    def _predict_sync(self, pcm_audio: bytes) -> SmartTurnPrediction:
        session, extractor, input_name, output_name = self._get_runtime()
        started = time.perf_counter()
        samples = np.frombuffer(pcm_audio, dtype="<i2").astype(np.float32) / 32768.0
        samples = samples[-WINDOW_SAMPLES:]
        if len(samples) < WINDOW_SAMPLES:
            samples = np.pad(samples, (WINDOW_SAMPLES - len(samples), 0))
        features = extractor(
            samples, sampling_rate=SAMPLE_RATE, return_tensors="np",
        )["input_features"]
        features = np.asarray(features, dtype=np.float32)
        if features.shape != (1, 80, 800):
            raise ValueError(
                f"Smart-Turn-Features haben unerwartete Form {features.shape}; erwartet (1, 80, 800)"
            )
        outputs = session.run([output_name], {input_name: features})
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        if not 0.0 <= probability <= 1.0:
            raise ValueError(
                f"Smart-Turn-Modell lieferte keine Wahrscheinlichkeit: {probability}"
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return SmartTurnPrediction(probability, round(elapsed_ms, 1))

    def _get_runtime(self):
        if self._load_error is not None:
            raise self._load_error
        if self._session is not None:
            return (
                self._session, self._feature_extractor,
                self._input_name, self._output_name,
            )
        with self._runtime_lock:
            if self._session is not None:
                return (
                    self._session, self._feature_extractor,
                    self._input_name, self._output_name,
                )
            if self._load_error is not None:
                raise self._load_error
            try:
                if not self.model_path.is_file():
                    raise FileNotFoundError(
                        f"Smart-Turn-Modell nicht gefunden: {self.model_path}"
                    )

                import onnxruntime as ort

                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                factory = self._session_factory or _create_onnx_session
                session = factory(
                    str(self.model_path),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
                inputs = session.get_inputs()
                outputs = session.get_outputs()
                if not inputs or not outputs:
                    raise ValueError("Smart-Turn-ONNX-Modell hat keine Ein- oder Ausgaben")
                feature_input = next(
                    (
                        item for item in inputs
                        if len(item.shape) == 3 and item.shape[-2:] == [80, 800]
                    ),
                    inputs[0],
                )
                extractor_factory = self._feature_extractor_factory or _create_feature_extractor
                extractor = extractor_factory()
            except Exception as exc:
                self._load_error = exc
                raise
            self._session = session
            self._feature_extractor = extractor
            self._input_name = feature_input.name
            self._output_name = outputs[0].name
            return session, extractor, self._input_name, self._output_name

    async def close(self) -> None:
        """Beendet den Worker, ohne den Event-Loop auf laufende Inferenz warten zu lassen."""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def _create_onnx_session(path, *, sess_options, providers):
    import onnxruntime as ort

    return ort.InferenceSession(
        path, sess_options=sess_options, providers=providers,
    )


def _create_feature_extractor():
    from transformers import WhisperFeatureExtractor

    return WhisperFeatureExtractor(chunk_length=WINDOW_SECONDS)


_default_analyzer: SmartTurnAnalyzer | None = None
_default_lock = threading.Lock()
_warned_errors: set[str] = set()


def get_smart_turn_analyzer() -> SmartTurnAnalyzer:
    """Liefert den pro Prozess gemeinsam genutzten Analyzer."""
    global _default_analyzer
    with _default_lock:
        if _default_analyzer is None:
            _default_analyzer = SmartTurnAnalyzer()
        return _default_analyzer


def warn_unavailable_once(error: Exception, model_path: str | Path | None = None) -> None:
    """Warnt pro Modellpfad einmal; Voice läuft danach mit der alten Heuristik weiter."""
    path = str(model_path or getattr(config, "VOICE_SMART_TURN_MODEL", "default"))
    with _default_lock:
        if path in _warned_errors:
            return
        _warned_errors.add(path)
    log.warning(
        "Smart Turn nicht verfügbar (%s); bisherige Stille-Logik wird verwendet",
        type(error).__name__,
    )
