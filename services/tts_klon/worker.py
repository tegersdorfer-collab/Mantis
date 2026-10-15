"""Isolierter JSONL-Worker für MLX Audio und Qwen3-TTS Voice Clone."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import base64
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path


MODEL_ID = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
_PROTOCOL_OUT = sys.stdout


def _emit(message: dict) -> None:
    _PROTOCOL_OUT.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    _PROTOCOL_OUT.flush()


def _watch_parent(parent_pid: int) -> None:
    """Beendet den Modellprozess kurz nach dem Verschwinden seines Elternprozesses."""
    while True:
        time.sleep(2)
        current_parent = os.getppid()
        if current_parent != parent_pid or current_parent <= 1:
            os._exit(0)


def _start_parent_watchdog() -> None:
    parent_pid = os.getppid()
    thread = threading.Thread(
        target=_watch_parent,
        args=(parent_pid,),
        name="tts-klon-parent-watchdog",
        daemon=True,
    )
    thread.start()


def _read_stdin(requests: queue.Queue[str | None]) -> None:
    """Leitet JSONL-Anfragen weiter und beendet sich direkt bei stdin-EOF."""
    for line in sys.stdin:
        requests.put(line)
    os._exit(0)


def _start_stdin_reader() -> queue.Queue[str | None]:
    requests: queue.Queue[str | None] = queue.Queue()
    thread = threading.Thread(
        target=_read_stdin,
        args=(requests,),
        name="tts-klon-stdin-reader",
        daemon=True,
    )
    thread.start()
    return requests


def _load_runtime(ref_dir: Path):
    import numpy as np
    from mlx_audio.tts.utils import load_model

    ref_audio = ref_dir / "nico_ref.wav"
    ref_text_path = ref_dir / "nico_ref.txt"
    if not ref_audio.is_file():
        raise FileNotFoundError(ref_audio)
    ref_text = ref_text_path.read_text(encoding="utf-8").strip()
    if not ref_text:
        raise ValueError("nico_ref.txt ist leer")

    with redirect_stdout(sys.stderr):
        model = load_model(MODEL_ID)
    return np, model, str(ref_audio), ref_text


def _pcm16le(audio, np) -> bytes:
    samples = np.asarray(audio).reshape(-1)
    if samples.dtype.kind in "iu":
        samples = np.clip(samples, -32768, 32767).astype("<i2", copy=False)
    else:
        samples = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    return samples.tobytes()


def main() -> int:
    _start_parent_watchdog()
    requests = _start_stdin_reader()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ref-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data" / "voice_clone",
    )
    args = parser.parse_args()

    try:
        np, model, ref_audio, ref_text = _load_runtime(args.ref_dir)
    except Exception as exc:
        startup_error = f"{type(exc).__name__}: {exc}"
        print(f"Klon-TTS-Worker konnte nicht starten: {startup_error}", file=sys.stderr, flush=True)
        _emit({"ready": False, "error": startup_error})
        return 1
    _emit({"ready": True})

    while True:
        line = requests.get()
        if line is None:
            return 0
        request_id = None
        try:
            request = json.loads(line)
            request_id = request["id"]
            text = str(request["text"]).strip()
            if not isinstance(request_id, (str, int)):
                raise ValueError("id muss ein String oder Integer sein")
            if not text:
                raise ValueError("text darf nicht leer sein")
            sequence = 0
            with redirect_stdout(sys.stderr):
                chunks = model.generate(
                    text=text,
                    lang_code="german",
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    stream=True,
                    streaming_interval=0.5,
                )
                for result in chunks:
                    pcm = _pcm16le(result.audio, np)
                    if not pcm:
                        continue
                    sample_rate = int(getattr(result, "sample_rate", model.sample_rate))
                    _emit({
                        "id": request_id,
                        "seq": sequence,
                        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                        "sample_rate": sample_rate,
                    })
                    sequence += 1
            _emit({"id": request_id, "done": True})
        except Exception as exc:
            _emit({"id": request_id, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
