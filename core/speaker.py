"""Lokale Sprecherverifikation mit NumPy-Fbank und einem ONNX-Modell."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Callable, Sequence

import numpy as np

import config
from core import db

SAMPLE_RATE = 16_000
FEATURE_BINS = 80
EMBEDDING_DIM = 192
MAX_EMBEDDINGS = 50
MAX_CENTROIDS = 4
AUTO_LEARN_INTERVAL_S = 600
DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "speaker" / "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
)
log = logging.getLogger(__name__)


def _unit(vector: np.ndarray) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(values))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Sprecher-Embedding ist leer oder ungültig")
    return values / norm


def kaldi_fbank(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Erzeugt sherpa-kompatible 80-bin Kaldi-Fbanks samt zeitlicher CMN."""
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"Sprecher-Fbank erwartet {SAMPLE_RATE} Hz")
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError("Sprecher-Fbank erwartet Mono-Audio")
    if not np.isfinite(audio).all():
        raise ValueError("Audio enthält ungültige Werte")

    frame_length = int(round(sample_rate * 0.025))
    frame_shift = int(round(sample_rate * 0.010))
    if len(audio) < frame_length:
        raise ValueError("Sprecher-Audio muss mindestens 25 ms lang sein")
    # SpeakerEmbeddingExtractor lässt die FeatureExtractorConfig-Defaults
    # bestehen: snip_edges=False und high_freq=-400 (Nyquist minus 400 Hz).
    frame_count = (len(audio) + frame_shift // 2) // frame_shift
    starts = (
        np.arange(frame_count, dtype=np.int64) * frame_shift
        - (frame_length - frame_shift) // 2
    )
    indices = starts[:, None] + np.arange(frame_length)[None, :]
    # Kaldi spiegelt Samples außerhalb des Signals an der Kante und wiederholt
    # dabei den Randwert.
    indices = np.where(indices < 0, -indices - 1, indices)
    indices = np.where(indices >= len(audio), 2 * len(audio) - indices - 1, indices)
    frames = audio[indices].copy()

    # Kaldi Fbank defaults used by sherpa-onnx: no snip_edges, no dither,
    # remove_dc_offset, pre-emphasis 0.97, Povey window, power spectrum.
    frames -= frames.mean(axis=1, keepdims=True)
    previous = frames[:, 0].copy()
    frames[:, 1:] -= np.float32(0.97) * frames[:, :-1].copy()
    frames[:, 0] -= np.float32(0.97) * previous
    window = np.power(
        0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame_length) / (frame_length - 1)),
        0.85,
    ).astype(np.float32)
    n_fft = 1 << (frame_length - 1).bit_length()
    spectrum = np.fft.rfft(frames * window[None, :], n=n_fft, axis=1)
    power = (spectrum.real * spectrum.real + spectrum.imag * spectrum.imag).astype(np.float32)

    def mel(frequency):
        return 1127.0 * np.log1p(np.asarray(frequency, dtype=np.float64) / 700.0)

    low_mel = mel(20.0)
    high_mel = mel(sample_rate / 2 - 400)
    mel_points = np.linspace(low_mel, high_mel, FEATURE_BINS + 2)
    frequencies = np.arange(n_fft // 2 + 1, dtype=np.float64) * sample_rate / n_fft
    bin_mel = mel(frequencies)
    lower = (bin_mel[None, :] - mel_points[:-2, None]) / (
        mel_points[1:-1, None] - mel_points[:-2, None]
    )
    upper = (mel_points[2:, None] - bin_mel[None, :]) / (
        mel_points[2:, None] - mel_points[1:-1, None]
    )
    weights = np.maximum(0.0, np.minimum(lower, upper)).astype(np.float32)
    energies = power @ weights.T
    features = np.log(np.maximum(energies, np.float32(1e-10)))
    features -= features.mean(axis=0, keepdims=True)
    return features.astype(np.float32, copy=False)


def cluster_centroids(embeddings: Sequence[np.ndarray]) -> np.ndarray:
    """Gruppiert Teilprofile deterministisch in höchstens vier Einheitszentroide."""
    vectors = np.stack([_unit(vector) for vector in embeddings]).astype(np.float32)
    if len(vectors) == 1:
        return vectors
    k = min(MAX_CENTROIDS, len(vectors))
    centers = [vectors[0]]
    while len(centers) < k:
        similarities = vectors @ np.stack(centers).T
        centers.append(vectors[int(np.argmin(similarities.max(axis=1)))])
    centers_array = np.stack(centers)
    for _ in range(30):
        assignments = np.argmax(vectors @ centers_array.T, axis=1)
        updated = []
        for index in range(k):
            members = vectors[assignments == index]
            if len(members):
                mean = members.mean(axis=0)
                norm = float(np.linalg.norm(mean))
                updated.append(mean / norm if norm > 1e-12 else centers_array[index])
            else:
                nearest = np.max(vectors @ centers_array.T, axis=1)
                updated.append(vectors[int(np.argmin(nearest))])
        updated_array = np.asarray(updated, dtype=np.float32)
        if np.allclose(updated_array, centers_array, atol=1e-6):
            centers_array = updated_array
            break
        centers_array = updated_array
    return centers_array.astype(np.float32, copy=False)


def score_embedding(embedding: np.ndarray, profile: Sequence[np.ndarray]) -> float:
    """Liefert den höchsten Cosinus zu einem der Profil-Zentroide."""
    if len(profile) == 0:
        raise ValueError("Sprecherprofil ist leer")
    target = _unit(embedding)
    centers = cluster_centroids(profile)
    if centers.shape[1] != target.size:
        raise ValueError("Sprecher-Embedding und Profil haben verschiedene Dimensionen")
    return float(np.clip(np.max(centers @ target), -1.0, 1.0))


def most_redundant_index(embeddings: Sequence[np.ndarray]) -> int:
    """Index des Teilprofils mit dem ähnlichsten Nachbarn (also redundantesten)."""
    vectors = np.stack([_unit(vector) for vector in embeddings])
    if len(vectors) <= 1:
        return 0
    similarities = vectors @ vectors.T
    np.fill_diagonal(similarities, -np.inf)
    return int(np.argmax(similarities.max(axis=1)))


def decode_wav(payload: bytes) -> tuple[np.ndarray, int]:
    """Dekodiert unkomprimiertes 16-bit PCM WAV in Mono-Float32 bei 16 kHz."""
    try:
        with wave.open(BytesIO(payload), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
            compression = source.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise ValueError("Ungültige WAV-Datei") from exc
    if channels != 1:
        raise ValueError("WAV muss mono sein")
    if sample_rate != SAMPLE_RATE:
        raise ValueError("WAV muss 16000 Hz haben")
    if width != 2 or compression != "NONE":
        raise ValueError("WAV muss unkomprimiertes 16-bit PCM sein")
    if len(frames) % 2:
        raise ValueError("WAV enthält unvollständige PCM-Daten")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if len(audio) < int(SAMPLE_RATE * 0.5):
        raise ValueError("WAV muss mindestens 0,5 Sekunden lang sein")
    return audio, sample_rate


def decode_pcm(pcm_audio: bytes) -> np.ndarray:
    if len(pcm_audio) % 2:
        raise ValueError("PCM-Audio enthält unvollständige 16-bit Samples")
    audio = np.frombuffer(pcm_audio, dtype="<i2").astype(np.float32) / 32768.0
    if len(audio) < int(SAMPLE_RATE * 0.025):
        raise ValueError("Sprecher-Audio muss mindestens 25 ms lang sein")
    return audio


def speech_duration(samples: np.ndarray, *, threshold_rms: float = 800 / 32768) -> float:
    """Schätzt aktive Sprechzeit mit 20-ms-RMS-Fenstern für Enrollment."""
    frame_samples = int(SAMPLE_RATE * 0.020)
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        return 0.0
    active = 0
    for start in range(0, len(audio), frame_samples):
        frame = audio[start:start + frame_samples]
        if len(frame) and float(np.sqrt(np.mean(frame * frame))) >= threshold_rms:
            active += len(frame)
    return active / SAMPLE_RATE


@dataclass(frozen=True)
class SpeakerVerification:
    score: float | None
    first_second_score: float | None
    elapsed_ms: float
    decision: str
    embedding: np.ndarray | None = None


@dataclass(frozen=True)
class SpeakerScore:
    score: float | None
    elapsed_ms: float
    enrolled: bool
    embedding: np.ndarray | None = None


class SpeakerProfileStore:
    """Persistiert ausschließlich Sprecher-Embeddings und Lernmetadaten."""

    def list_embeddings(self) -> list[dict]:
        return db.query(
            """SELECT id, embedding, score, source, learned_at
               FROM speaker_profile ORDER BY id"""
        )

    def add_embedding(
        self,
        embedding: np.ndarray,
        score: float | None,
        source: str,
        *,
        minimum_interval_s: int | None = None,
    ) -> bool:
        vector = _unit(embedding)
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(5283301)")
            cur.execute(
                "SELECT id, embedding, source, learned_at FROM speaker_profile ORDER BY id"
            )
            rows = [dict(row) for row in cur.fetchall()]
            now = datetime.now(timezone.utc)
            if minimum_interval_s is not None:
                auto_rows = [row for row in rows if row["source"] == source]
                if auto_rows:
                    last_at = max(row["learned_at"] for row in auto_rows)
                    if (now - last_at).total_seconds() < minimum_interval_s:
                        return False
            if len(rows) >= MAX_EMBEDDINGS:
                redundant = most_redundant_index([row["embedding"] for row in rows])
                cur.execute("DELETE FROM speaker_profile WHERE id = %s", (rows[redundant]["id"],))
            cur.execute(
                """INSERT INTO speaker_profile (embedding, score, source)
                   VALUES (%s, %s, %s)""",
                (vector.tolist(), score, source),
            )
        return True

    def history(self, limit: int = 10) -> list[dict]:
        return db.query(
            """SELECT score, source, learned_at FROM speaker_profile
               ORDER BY learned_at DESC, id DESC LIMIT %s""",
            (max(1, min(50, int(limit))),),
        )

    def clear(self) -> None:
        db.execute("DELETE FROM speaker_profile")


class SpeakerVerifier:
    """Ein ONNX-Sprecher-Embedder mit eigenem Single-Thread-Executor."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        session_factory: Callable | None = None,
        embedder: Callable[[np.ndarray, int], np.ndarray] | None = None,
        store: SpeakerProfileStore | None = None,
    ):
        configured = model_path or getattr(config, "VOICE_SPEAKER_MODEL", DEFAULT_MODEL_PATH)
        self.model_path = Path(configured).expanduser()
        self._session_factory = session_factory
        self._fake_embedder = embedder
        self._store = store or SpeakerProfileStore()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-speaker")
        self._session = None
        self._input_name: str | None = None
        self._output_name: str | None = None
        self._closed = False
        self._load_error: Exception | None = None
        self._runtime_lock = threading.Lock()

    async def embed_waveform(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        if self._closed:
            raise RuntimeError("SpeakerVerifier ist geschlossen")
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"Sprecher-Embedding erwartet {SAMPLE_RATE} Hz")
        audio = np.asarray(samples, dtype=np.float32).copy()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._embed_sync, audio, sample_rate)

    async def embed_pcm(self, pcm_audio: bytes) -> np.ndarray:
        return await self.embed_waveform(decode_pcm(pcm_audio), SAMPLE_RATE)

    async def warmup(self) -> None:
        audio = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
        await self.embed_waveform(audio, SAMPLE_RATE)

    def _embed_sync(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if self._fake_embedder is not None:
            return _unit(self._fake_embedder(audio, sample_rate))
        session, input_name, output_name = self._get_runtime()
        features = kaldi_fbank(audio, sample_rate)
        result = session.run([output_name], {input_name: features[np.newaxis, :, :]})
        embedding = np.asarray(result[0], dtype=np.float32).reshape(-1)
        if embedding.size != EMBEDDING_DIM:
            raise ValueError(
                f"Sprecher-Modell lieferte {embedding.size} statt {EMBEDDING_DIM} Werte"
            )
        return _unit(embedding)

    def _get_runtime(self):
        if self._load_error is not None:
            raise self._load_error
        if self._session is not None:
            return self._session, self._input_name, self._output_name
        with self._runtime_lock:
            if self._session is not None:
                return self._session, self._input_name, self._output_name
            if not self.model_path.is_file():
                raise FileNotFoundError(f"Sprecher-Modell nicht gefunden: {self.model_path}")
            try:
                if self._session_factory is None:
                    import onnxruntime as ort

                    options = ort.SessionOptions()
                    factory = ort.InferenceSession
                else:
                    options = SimpleNamespace()
                    factory = self._session_factory
                options.intra_op_num_threads = 1
                options.inter_op_num_threads = 1
                session = factory(
                    str(self.model_path), sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
                inputs, outputs = session.get_inputs(), session.get_outputs()
                if not inputs or not outputs:
                    raise ValueError("Sprecher-ONNX-Modell hat keine Ein- oder Ausgaben")
            except Exception as exc:
                self._load_error = exc
                raise
            self._session = session
            self._input_name = inputs[0].name
            self._output_name = outputs[0].name
            return session, self._input_name, self._output_name

    async def _rows(self) -> list[dict]:
        return await asyncio.to_thread(self._store.list_embeddings)

    async def has_profile(self) -> bool:
        return bool(await self._rows())

    async def status(self) -> dict:
        rows = await self._rows()
        history = await asyncio.to_thread(self._store.history, 10)
        return {
            "enrolled": bool(rows),
            "embeddings": len(rows),
            "last_learning": [
                {
                    "learned_at": _as_iso(item.get("learned_at")),
                    "score": item.get("score"),
                    "source": item.get("source"),
                }
                for item in history
            ],
        }

    async def reset(self) -> None:
        await asyncio.to_thread(self._store.clear)

    async def score_pcm(self, pcm_audio: bytes) -> SpeakerScore:
        rows = await self._rows()
        if not rows:
            return SpeakerScore(None, 0.0, False)
        started = time.perf_counter()
        embedding = await self.embed_pcm(pcm_audio)
        score = score_embedding(embedding, [row["embedding"] for row in rows])
        return SpeakerScore(score, round((time.perf_counter() - started) * 1000, 1), True, embedding)

    async def verify_pcm(self, pcm_audio: bytes) -> SpeakerVerification:
        rows = await self._rows()
        if not rows:
            return SpeakerVerification(None, None, 0.0, "no_profile")
        audio = decode_pcm(pcm_audio)
        started = time.perf_counter()
        full_embedding = await self.embed_waveform(audio, SAMPLE_RATE)
        full_score = score_embedding(full_embedding, [row["embedding"] for row in rows])
        first_second = audio[:SAMPLE_RATE]
        if len(audio) <= SAMPLE_RATE:
            first_score = full_score
        else:
            first_embedding = await self.embed_waveform(first_second, SAMPLE_RATE)
            first_score = score_embedding(first_embedding, [row["embedding"] for row in rows])
        score = min(full_score, first_score)
        threshold = float(getattr(config, "VOICE_SPEAKER_THRESHOLD", 0.38))
        return SpeakerVerification(
            score=score,
            first_second_score=first_score,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            decision="accepted" if full_score >= threshold and first_score >= threshold else "rejected",
            embedding=full_embedding,
        )

    async def maybe_learn(
        self,
        pcm_audio: bytes,
        score: float,
        *,
        acoustic_wakeword: bool,
        bargein: bool,
        overlapping: bool,
        embedding: np.ndarray | None = None,
    ) -> bool:
        if not acoustic_wakeword or bargein or overlapping:
            return False
        if score < float(getattr(config, "VOICE_SPEAKER_LEARN_MIN", 0.6)):
            return False
        audio = decode_pcm(pcm_audio)
        if speech_duration(audio) < 1.5:
            return False
        vector = embedding if embedding is not None else await self.embed_waveform(audio, SAMPLE_RATE)
        return await asyncio.to_thread(
            self._store.add_embedding, vector, score, "wakeword",
            minimum_interval_s=AUTO_LEARN_INTERVAL_S,
        )

    async def enroll_wavs(
        self,
        wav_payloads: Sequence[bytes],
        *,
        minimum_speech_seconds: float = 20.0,
    ) -> dict:
        if not wav_payloads:
            raise ValueError("Mindestens ein WAV-Stück ist erforderlich")
        decoded = [decode_wav(payload) for payload in wav_payloads]
        total_speech = sum(speech_duration(audio) for audio, _rate in decoded)
        if total_speech < minimum_speech_seconds:
            raise ValueError(
                f"Mindestens 20 Sekunden Sprache erforderlich (erkannt: {total_speech:.1f} s)"
            )
        added = 0
        for audio, sample_rate in decoded:
            embedding = await self.embed_waveform(audio, sample_rate)
            did_add = await asyncio.to_thread(
                self._store.add_embedding, embedding, None, "enrollment",
            )
            added += int(bool(did_add))
        status = await self.status()
        return {"embeddings_added": added, **status}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def _as_iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


_default_verifier: SpeakerVerifier | None = None
_default_lock = threading.Lock()
_warned_missing: set[str] = set()


def get_speaker_verifier() -> SpeakerVerifier:
    global _default_verifier
    with _default_lock:
        if _default_verifier is None:
            _default_verifier = SpeakerVerifier()
        return _default_verifier


async def preload_speaker(after_task: asyncio.Task | None = None) -> None:
    if after_task is not None:
        await asyncio.gather(after_task, return_exceptions=True)
    verifier = get_speaker_verifier()
    try:
        await verifier.warmup()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        key = str(verifier.model_path)
        if key not in _warned_missing:
            _warned_missing.add(key)
            log.warning(
                "Sprecherverifikation nicht verfügbar (%s): Modellpfad %s",
                type(exc).__name__, verifier.model_path,
            )


def schedule_speaker_preload(after_task: asyncio.Task | None = None) -> asyncio.Task:
    return asyncio.create_task(preload_speaker(after_task), name="speaker-model-preload")
