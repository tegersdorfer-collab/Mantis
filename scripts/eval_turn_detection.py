#!/usr/bin/env python3
"""Vergleicht Smart Turn und die bestehende Stille-Heuristik offline."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from core.smart_turn import SmartTurnAnalyzer
from core.vad import SAMPLE_RATE, UtteranceStatus, classify_utterance

SILENCE_CANDIDATES_MS = (250, 450, 800, 1100, 1400, 1700, 2000)
VAD_FRAME_SAMPLES = 512
QUIET_RMS_THRESHOLD = 200.0
MIN_INTERNAL_GAP_MS = 200


def heuristic_silence_ms(
    text: str,
    *,
    fast_ms: int = 450,
    slow_ms: int = 2000,
    base_ms: int = 800,
) -> int:
    status = classify_utterance(text)
    if status is UtteranceStatus.COMPLETE:
        return fast_ms
    if status is UtteranceStatus.INCOMPLETE:
        return slow_ms
    return base_ms


def checkpoint_audio(pcm_audio: bytes, *, speech_end_sample: int, silence_ms: int) -> bytes:
    end_sample = max(0, int(speech_end_sample))
    prefix = bytes(pcm_audio[:end_sample * 2])
    silence_samples = max(0, int(SAMPLE_RATE * max(0, silence_ms) / 1000))
    return prefix + b"\0\0" * silence_samples


def internal_quiet_gaps(
    pcm_audio: bytes,
    *,
    speech_end_sample: int,
    frame_samples: int = VAD_FRAME_SAMPLES,
    rms_threshold: float = QUIET_RMS_THRESHOLD,
    min_gap_ms: int = MIN_INTERNAL_GAP_MS,
) -> list[tuple[int, int]]:
    """Liefert leise Innenpausen, auf die vor dem letzten Sprachframe wieder Sprache folgt."""
    samples = np.frombuffer(pcm_audio, dtype="<i2")
    limit = min(len(samples), max(0, int(speech_end_sample)))
    voiced_seen = False
    gap_start: int | None = None
    gaps: list[tuple[int, int]] = []
    minimum_samples = int(SAMPLE_RATE * min_gap_ms / 1000)
    for start in range(0, limit, frame_samples):
        block = samples[start:min(limit, start + frame_samples)].astype(np.float32)
        if not len(block):
            continue
        is_quiet = float(np.sqrt(np.mean(block * block))) < rms_threshold
        if is_quiet and voiced_seen:
            if gap_start is None:
                gap_start = start
            continue
        if not is_quiet:
            if (
                gap_start is not None
                and start - gap_start >= minimum_samples
            ):
                gaps.append((gap_start, start))
            gap_start = None
            voiced_seen = True
    return gaps


def _load_capture(wav_path: Path) -> tuple[bytes, dict]:
    json_path = wav_path.with_suffix(".json")
    metadata = json.loads(json_path.read_text(encoding="utf-8"))
    with wave.open(str(wav_path), "rb") as wav_file:
        if (
            wav_file.getnchannels() != 1
            or wav_file.getsampwidth() != 2
            or wav_file.getframerate() != SAMPLE_RATE
        ):
            raise ValueError(f"Nicht unterstütztes WAV-Format: {wav_path}")
        audio = wav_file.readframes(wav_file.getnframes())
    return audio, metadata


def _partial_text_at(
    metadata: dict,
    sample_index: int,
    audio: bytes,
    *,
    allow_untimed_partial: bool = False,
) -> str:
    offset_ms = max(0, sample_index) * 1000 / SAMPLE_RATE
    partials = metadata.get("partial_transcripts") or []
    eligible = [
        item for item in partials
        if float(item.get("offset_ms", 0)) <= offset_ms
    ]
    if eligible:
        latest = max(eligible, key=lambda item: float(item.get("offset_ms", 0)))
        return str(latest.get("text") or "")
    if allow_untimed_partial:
        return str(metadata.get("partial_text") or "")
    return ""


def _model_decision(probability: float) -> str:
    if probability >= float(config.VOICE_SMART_TURN_COMPLETE):
        return "complete"
    if probability < float(config.VOICE_SMART_TURN_INCOMPLETE):
        return "incomplete"
    return "heuristic"


async def _evaluate_checkpoint(
    analyzer: SmartTurnAnalyzer,
    pcm_audio: bytes,
    *,
    speech_end_sample: int,
    silence_ms: int,
    point: str,
    heuristic_text: str,
    capture_name: str,
) -> dict:
    checkpoint = checkpoint_audio(
        pcm_audio, speech_end_sample=speech_end_sample, silence_ms=silence_ms,
    )
    prediction = await analyzer.predict(checkpoint)
    return {
        "capture": capture_name,
        "point": point,
        "silence_ms": silence_ms,
        "probability": prediction.probability,
        "model": _model_decision(prediction.probability),
        "heuristic": classify_utterance(heuristic_text).value.lower(),
        "heuristic_delay_ms": heuristic_silence_ms(heuristic_text),
    }


async def evaluate_directory(
    captures_dir: Path,
    analyzer: SmartTurnAnalyzer,
) -> list[dict]:
    rows = []
    for wav_path in sorted(captures_dir.rglob("*.wav")):
        audio, metadata = _load_capture(wav_path)
        audio_samples = len(audio) // 2
        speech_end = int(metadata.get("last_speech_sample_index") or audio_samples)
        speech_end = min(audio_samples, max(0, speech_end))
        terminal_text = _partial_text_at(
            metadata, speech_end, audio, allow_untimed_partial=True,
        )
        capture_name = wav_path.stem
        for silence_ms in SILENCE_CANDIDATES_MS:
            rows.append(await _evaluate_checkpoint(
                analyzer,
                audio,
                speech_end_sample=speech_end,
                silence_ms=silence_ms,
                point="segmentende",
                heuristic_text=terminal_text,
                capture_name=capture_name,
            ))

        for gap_start, gap_end in internal_quiet_gaps(
            audio, speech_end_sample=speech_end,
        ):
            gap_ms = int((gap_end - gap_start) * 1000 / SAMPLE_RATE)
            for silence_ms in SILENCE_CANDIDATES_MS:
                if silence_ms > gap_ms:
                    break
                pause_text = _partial_text_at(metadata, gap_start, audio)
                rows.append(await _evaluate_checkpoint(
                    analyzer,
                    audio,
                    speech_end_sample=gap_start,
                    silence_ms=silence_ms,
                    point="interne_Pause",
                    heuristic_text=pause_text,
                    capture_name=capture_name,
                ))
    return rows


def _print_table(rows: list[dict]) -> None:
    headers = ("Aufnahme", "Stelle", "Stille", "p fertig", "Smart Turn", "Heuristik", "Heuristik ms")
    print(" | ".join(headers))
    print("-+-".join("-" * len(value) for value in headers))
    for row in rows:
        print(
            f"{row['capture'][:20]:20} | {row['point']:13} | "
            f"{row['silence_ms']:6} | {row['probability']:7.3f} | "
            f"{row['model']:11} | {row['heuristic']:10} | "
            f"{row['heuristic_delay_ms']:12}"
        )


async def _run(args) -> int:
    wav_paths = list(args.captures.rglob("*.wav"))
    if not wav_paths:
        print(f"Keine WAV-Mitschnitte in {args.captures}")
        return 0
    analyzer = SmartTurnAnalyzer(args.model)
    try:
        rows = await evaluate_directory(args.captures, analyzer)
        _print_table(rows)
    finally:
        await analyzer.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--captures", type=Path,
        default=REPO_ROOT / "data" / "voice_segments",
        help="Mitschnitt-Wurzel (Standard: data/voice_segments)",
    )
    parser.add_argument(
        "--model", type=Path, default=Path(config.VOICE_SMART_TURN_MODEL),
        help="lokale Smart-Turn-ONNX-Datei",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
