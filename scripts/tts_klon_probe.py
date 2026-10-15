"""Manueller Live-Probe für den echten, isolierten Klon-TTS-Worker."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


SENTENCES = (
    "Der Branch ist gemerged, und das Deployment läuft auf dem Server.",
    "Okay, ich prüfe das Update und starte den Timer für das Meeting.",
)


async def _rss_megabytes(pid: int) -> float | None:
    process = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "rss=",
        "-p",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    try:
        return float(output.decode("ascii").strip()) / 1024
    except ValueError:
        return None


async def _sample_rss(pid: int, samples: list[float], finished: asyncio.Event) -> None:
    while not finished.is_set():
        rss = await _rss_megabytes(pid)
        if rss is not None:
            samples.append(rss)
        try:
            await asyncio.wait_for(finished.wait(), timeout=0.2)
        except TimeoutError:
            pass


async def _probe_sentence(process, request_id: str, text: str) -> tuple[float, float, float]:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("Worker ohne Standard-Streams gestartet")
    started = time.perf_counter()
    process.stdin.write(
        (json.dumps({"id": request_id, "text": text}, ensure_ascii=False) + "\n").encode()
    )
    await process.stdin.drain()

    first_chunk_at = None
    audio_seconds = 0.0
    while True:
        line = await process.stdout.readline()
        if not line:
            raise RuntimeError(f"Worker endete unerwartet (Exit {process.returncode})")
        result = json.loads(line)
        if result.get("id") != request_id:
            raise RuntimeError("Worker lieferte eine falsche Anfrage-ID")
        if "error" in result:
            raise RuntimeError(result["error"])
        if result.get("done"):
            break
        if first_chunk_at is None:
            first_chunk_at = time.perf_counter()
        pcm = base64.b64decode(result["pcm_b64"], validate=True)
        audio_seconds += len(pcm) / 2 / int(result["sample_rate"])

    elapsed = time.perf_counter() - started
    ttfa = (first_chunk_at or time.perf_counter()) - started
    rtf = elapsed / audio_seconds if audio_seconds else float("inf")
    return ttfa, rtf, audio_seconds


async def main() -> int:
    python = Path(config.TTS_KLON_PYTHON).expanduser()
    ref_dir = Path(config.TTS_KLON_REF_DIR).expanduser()
    worker = ROOT / "services" / "tts_klon" / "worker.py"
    if not python.is_file():
        raise SystemExit(f"Klon-Python nicht gefunden: {python}")
    if not worker.is_file():
        raise SystemExit(f"Klon-Worker nicht gefunden: {worker}")
    if not (ref_dir / "nico_ref.wav").is_file() or not (ref_dir / "nico_ref.txt").is_file():
        raise SystemExit(f"Klon-Referenzdateien fehlen in {ref_dir}")

    process = await asyncio.create_subprocess_exec(
        str(python),
        str(worker),
        "--ref-dir",
        str(ref_dir),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rss_samples: list[float] = []
    finished = asyncio.Event()
    sampler = asyncio.create_task(_sample_rss(process.pid, rss_samples, finished))
    try:
        for index, sentence in enumerate(SENTENCES, start=1):
            ttfa, rtf, audio_seconds = await _probe_sentence(
                process,
                str(index),
                sentence,
            )
            peak_rss = max(rss_samples, default=0.0)
            print(
                f"Satz {index}: TTFA {ttfa:.2f}s | RTF {rtf:.2f} | "
                f"Audio {audio_seconds:.2f}s | RSS-Peak {peak_rss / 1024:.2f} GiB",
                flush=True,
            )
    finally:
        finished.set()
        await sampler
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
