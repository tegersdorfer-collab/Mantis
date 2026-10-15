"""Cloud-TTS mit konfigurierbarer Ersatzkette und lokalem Kokoro/Piper-Fallback.

Alle öffentlichen Funktionen liefern Telegram-kompatible OGG/Opus-Bytes. Cloud-
Provider-Audio wird bei Bedarf über dieselbe ffmpeg-Konvertierung wie der lokale
Pfad umgewandelt. Lokale Modelle werden erst geladen, wenn Cloud-Provider ausfallen.
"""
from __future__ import annotations
import asyncio
import base64
import binascii
import contextvars
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import config
from core import db
from core.error_redaction import redact_error_text
import httpx

log = logging.getLogger(__name__)

try:
    from num2words import num2words as _num2words
except ImportError:  # num2words ist optional; der Fallback deckt die üblichen Zahlen ab.
    _num2words = None

_MODEL_DIR = Path(__file__).parent.parent / "data" / "tts" / "piper"
_KOKORO_DIR = Path(__file__).parent.parent / "data" / "tts" / "kokoro"
_ESPEAK_LIB = "/opt/homebrew/lib/libespeak-ng.dylib"
_ESPEAK_DATA = "/opt/homebrew/share/espeak-ng-data"
_CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2025-04-16"
_CARTESIA_TIMEOUT_S = 5.0
_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_ELEVENLABS_TIMEOUT_S = 5.0
_ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
_GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_GOOGLE_TTS_TIMEOUT_S = 5.0
_GOOGLE_TTS_MODEL = "chirp3-hd"
_GOOGLE_TTS_USD_PER_MILLION_CHARS = 30.0
_DEFAULT_ENGINE_CHAIN = ("cartesia", "klon", "elevenlabs", "google", "local")
_LEGACY_DEFAULT_ENGINE_CHAIN = ("cartesia", "elevenlabs", "google", "local")
_ERROR_COOLDOWN_S = 60.0
_BOUND_ENGINE_RETRY_TIMEOUT_S = 1.5
_KLON_START_TIMEOUT_DEFAULT_S = 20.0
_KLON_FIRST_CHUNK_TIMEOUT_DEFAULT_S = 3.0
_KLON_CHUNK_TIMEOUT_DEFAULT_S = 3.0
_QUOTA_HINTS = ("quota", "credit", "payment_required")
_KLON_MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
_KLON_WORKER_PATH = Path(__file__).parent.parent / "services" / "tts_klon" / "worker.py"

VOICE_MODELS: dict[str, str] = {
    "thorsten-high": "de_DE-thorsten-high",
    "thorsten_emotional-medium": "de_DE-thorsten_emotional-medium",
    "karlsson-low": "de_DE-karlsson-low",
    "pavoque-low": "de_DE-pavoque-low",
}

DEFAULT_VOICE = "thorsten-high"  # männlich, deutsch — unverändert ggü. bisherigem Verhalten
DEFAULT_SPEED = 1.0

_voice: object | None = None
_loaded_voice_name: str | None = None
_kokoro: object | None = None
# Wird gesetzt, sobald Kokoro nachweislich nicht läuft (fehlendes Paket, fehlendes
# Modell, fehlendes espeak-ng). Danach geht alles über Piper, statt bei jeder
# Antwort erneut in denselben Fehler zu laufen.
_kokoro_unavailable = False
_cartesia_paused_until = 0.0
_cartesia_pause_warned = False
_elevenlabs_paused_until = 0.0
_elevenlabs_pause_warned = False
_google_tts_paused_until = 0.0
_google_tts_pause_warned = False
_engine_paused_until: dict[str, float] = {}
_engine_error_paused_until: dict[str, float] = {}
_engine_quota_paused_until: dict[str, float] = {}
_engine_pause_warned: set[str] = set()
_lock = asyncio.Lock()
_klon_process: asyncio.subprocess.Process | None = None
_klon_process_lock = asyncio.Lock()
_klon_idle_task: asyncio.Task | None = None
_klon_prewarm_task: asyncio.Task | None = None
_klon_last_used = 0.0
_klon_request_seq = 0


async def _await_native_worker(awaitable):
    """Hält den TTS-Modell-Lock bis ein nicht abbrechbarer Thread fertig ist."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException:
            pass
        raise


def _cancel_klon_idle_task() -> None:
    global _klon_idle_task
    task = _klon_idle_task
    _klon_idle_task = None
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _discard_klon_worker(process: asyncio.subprocess.Process) -> None:
    global _klon_process
    if _klon_process is process:
        _klon_process = None
        _cancel_klon_idle_task()
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            log.error("Klon-TTS-Worker ließ sich nicht rechtzeitig reap'en")


def _klon_timeout(config_name: str, default: float) -> float:
    try:
        return max(0.01, float(getattr(config, config_name, default)))
    except (TypeError, ValueError):
        return default


def _klon_worker_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "HF_HOME", "HF_HUB_CACHE", "TMPDIR", "LANG"}
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed or name.startswith("LC_")
    }


async def _expire_klon_worker(
    process: asyncio.subprocess.Process,
    last_used: float,
    idle_s: float,
) -> None:
    await asyncio.sleep(idle_s)
    async with _klon_process_lock:
        if (
            _klon_process is process
            and time.monotonic() - _klon_last_used >= idle_s
            and _klon_last_used == last_used
        ):
            await _discard_klon_worker(process)
            log.info("Klon-TTS nach %.0f s Inaktivität beendet", idle_s)


def _touch_klon_worker(process: asyncio.subprocess.Process) -> None:
    global _klon_idle_task, _klon_last_used
    _klon_last_used = time.monotonic()
    idle_s = max(0.01, float(getattr(config, "TTS_KLON_IDLE_S", 600.0)))
    _cancel_klon_idle_task()
    _klon_idle_task = asyncio.create_task(
        _expire_klon_worker(process, _klon_last_used, idle_s),
        name="mantis-tts-klon-idle",
    )


async def _ensure_klon_worker() -> asyncio.subprocess.Process:
    global _klon_process
    if not _klon_is_configured():
        raise RuntimeError("Klon-TTS-Worker, Python-Umgebung oder Referenzdateien fehlen")
    if _klon_process is not None and _klon_process.returncode is None:
        return _klon_process

    _cancel_klon_idle_task()
    python = _klon_python_path()
    if python is None:
        raise RuntimeError("TTS_KLON_PYTHON ist nicht verfügbar")
    process = await asyncio.create_subprocess_exec(
        str(python),
        str(_KLON_WORKER_PATH),
        "--ref-dir",
        str(_klon_ref_dir()),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_klon_worker_environment(),
    )
    _klon_process = process
    try:
        if process.stdout is None:
            raise RuntimeError("Klon-TTS-Worker ohne Standardausgabe gestartet")
        line = await asyncio.wait_for(
            process.stdout.readline(),
            timeout=_klon_timeout("TTS_KLON_START_TIMEOUT_S", _KLON_START_TIMEOUT_DEFAULT_S),
        )
        if not line:
            raise RuntimeError("Klon-TTS-Worker endete vor dem Modell-Start")
        try:
            message = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Klon-TTS-Worker meldete einen ungültigen Startstatus") from exc
        if not isinstance(message, dict) or message.get("ready") is not True:
            detail = message.get("error") if isinstance(message, dict) else None
            raise RuntimeError(str(detail or "Klon-TTS-Worker meldete keinen erfolgreichen Start"))
    except BaseException:
        await _discard_klon_worker(process)
        raise
    _touch_klon_worker(process)
    log.info("Klon-TTS-Worker gestartet")
    return process


async def _stop_klon_worker() -> None:
    global _klon_prewarm_task
    prewarm_task = _klon_prewarm_task
    if prewarm_task is not None and prewarm_task is not asyncio.current_task():
        if not prewarm_task.done():
            prewarm_task.cancel()
            await asyncio.gather(prewarm_task, return_exceptions=True)
        if _klon_prewarm_task is prewarm_task:
            _klon_prewarm_task = None
    async with _klon_process_lock:
        process = _klon_process
        if process is not None:
            await _discard_klon_worker(process)
        else:
            _cancel_klon_idle_task()


async def _prewarm_klon() -> None:
    """Startet den isolierten Prozess vor dem ersten Fallback-Aufruf."""
    if not _klon_is_configured():
        return
    async with _klon_process_lock:
        process = await _ensure_klon_worker()
        _touch_klon_worker(process)


def _schedule_klon_prewarm() -> None:
    global _klon_prewarm_task
    if _klon_prewarm_task is not None and not _klon_prewarm_task.done():
        return
    try:
        task = asyncio.create_task(_prewarm_klon(), name="mantis-tts-klon-prewarm")
    except RuntimeError:
        return
    _klon_prewarm_task = task

    def report_failure(done_task: asyncio.Task) -> None:
        global _klon_prewarm_task
        if _klon_prewarm_task is done_task:
            _klon_prewarm_task = None
        if done_task.cancelled():
            return
        error = done_task.exception()
        if error is not None:
            log.warning("Klon-TTS konnte nicht vorgewärmt werden (%s)", type(error).__name__)

    task.add_done_callback(report_failure)


async def _klon_pcm_stream(text: str):
    """Liefert int16-LE-PCM-Chunks aus dem isolierten Klon-Worker."""
    global _klon_request_seq
    async with _klon_process_lock:
        process = await _ensure_klon_worker()
        if process.stdin is None or process.stdout is None:
            await _discard_klon_worker(process)
            raise RuntimeError("Klon-TTS-Worker ohne Standard-Streams gestartet")

        _klon_request_seq += 1
        request_id = str(_klon_request_seq)
        request = {"id": request_id, "text": text}
        expected_seq = 0
        sample_rate = None
        got_chunk = False
        completed = False
        first_chunk_deadline = (
            asyncio.get_running_loop().time()
            + _klon_timeout(
                "TTS_KLON_FIRST_CHUNK_TIMEOUT_S",
                _KLON_FIRST_CHUNK_TIMEOUT_DEFAULT_S,
            )
        )
        try:
            process.stdin.write(
                (
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            )
            await asyncio.wait_for(
                process.stdin.drain(),
                timeout=max(0.0, first_chunk_deadline - asyncio.get_running_loop().time()),
            )
            while True:
                if got_chunk:
                    timeout = _klon_timeout(
                        "TTS_KLON_CHUNK_TIMEOUT_S",
                        _KLON_CHUNK_TIMEOUT_DEFAULT_S,
                    )
                else:
                    timeout = max(
                        0.0,
                        first_chunk_deadline - asyncio.get_running_loop().time(),
                    )
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                if not line:
                    raise RuntimeError("Klon-TTS-Worker wurde unerwartet beendet")
                try:
                    message = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("Klon-TTS-Worker lieferte ungültiges JSON") from exc
                if not isinstance(message, dict):
                    raise RuntimeError("Klon-TTS-Worker lieferte keine JSON-Objektzeile")
                if message.get("id") != request_id:
                    raise RuntimeError("Klon-TTS-Worker lieferte eine falsche Anfrage-ID")
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                if message.get("done") is True:
                    completed = True
                    break

                try:
                    sequence = int(message["seq"])
                    chunk_rate = int(message["sample_rate"])
                    pcm = base64.b64decode(message["pcm_b64"], validate=True)
                except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                    raise RuntimeError("Klon-TTS-Worker lieferte einen ungültigen Audio-Chunk") from exc
                if sequence != expected_seq or chunk_rate <= 0 or not pcm or len(pcm) % 2:
                    raise RuntimeError("Klon-TTS-Worker lieferte leeres oder fehlerhaftes PCM")
                if sample_rate is not None and chunk_rate != sample_rate:
                    raise RuntimeError("Klon-TTS-Worker änderte die Sample-Rate im Stream")
                expected_seq += 1
                sample_rate = chunk_rate
                got_chunk = True
                _touch_klon_worker(process)
                yield pcm, chunk_rate
            if not got_chunk:
                raise RuntimeError("Klon-TTS-Worker lieferte kein Audio")
            _touch_klon_worker(process)
        except BaseException:
            await _discard_klon_worker(process)
            raise
        finally:
            if completed and _klon_process is process:
                _touch_klon_worker(process)


@dataclass(slots=True)
class TTSTurnState:
    """Providerbindung für alle Satzblöcke eines Voice-Turns."""

    engine: str | None = None
    chain_position: int | None = None
    audio_emitted: bool = False


class TTSSynthesisIncompleteError(RuntimeError):
    """Ein TTS-Textteil fehlte; synthesized_text enthält den fertigen Präfix."""

    def __init__(self, message: str, *, synthesized_text: str):
        super().__init__(message)
        self.synthesized_text = synthesized_text.strip()


_active_tts_turn: contextvars.ContextVar[TTSTurnState | None] = contextvars.ContextVar(
    "active_tts_turn",
    default=None,
)


def _engine() -> str:
    return getattr(config, "TTS_ENGINE", "cartesia")


def _engine_chain() -> list[str]:
    """Liefert die konfigurierte Kette und berücksichtigt alte TTS_ENGINE-Werte."""
    configured = list(getattr(config, "TTS_ENGINE_CHAIN", _DEFAULT_ENGINE_CHAIN))
    explicit = bool(getattr(config, "TTS_ENGINE_CHAIN_EXPLICIT", False))
    if not explicit and tuple(configured) in {
        _DEFAULT_ENGINE_CHAIN,
        _LEGACY_DEFAULT_ENGINE_CHAIN,
    }:
        primary = str(_engine() or "cartesia").strip().lower()
        if primary in {"kokoro", "piper"}:
            return [primary]
        configured = [primary, *(name for name in _DEFAULT_ENGINE_CHAIN if name != primary)]

    chain: list[str] = []
    for value in configured:
        name = str(value).strip().lower()
        if name and name not in chain:
            chain.append(name)
    return chain


def _cartesia_key() -> str:
    return str(getattr(config, "CARTESIA_API_KEY", "") or "").strip()


def _elevenlabs_key() -> str:
    return str(getattr(config, "ELEVENLABS_API_KEY", "") or "").strip()


def _google_tts_key() -> str:
    return str(getattr(config, "GOOGLE_TTS_API_KEY", "") or "").strip()


def _klon_python_path() -> Path | None:
    configured = str(getattr(config, "TTS_KLON_PYTHON", "") or "").strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if candidate.is_file():
        return candidate
    resolved = shutil.which(configured)
    return Path(resolved) if resolved else None


def _klon_ref_dir() -> Path:
    configured = str(getattr(config, "TTS_KLON_REF_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).parent.parent / "data" / "voice_clone"


def _klon_is_configured() -> bool:
    python = _klon_python_path()
    ref_dir = _klon_ref_dir()
    ref_audio = ref_dir / "nico_ref.wav"
    ref_text = ref_dir / "nico_ref.txt"
    return bool(
        python
        and python.is_file()
        and os.access(python, os.X_OK)
        and _KLON_WORKER_PATH.is_file()
        and ref_audio.is_file()
        and ref_audio.stat().st_size > 0
        and ref_text.is_file()
        and ref_text.stat().st_size > 0
    )


def _pause_until_compat_name(engine: str) -> str:
    return {
        "cartesia": "_cartesia_paused_until",
        "elevenlabs": "_elevenlabs_paused_until",
        "google": "_google_tts_paused_until",
    }.get(engine, "")


def _engine_label(engine: str) -> str:
    return {
        "cartesia": "Cartesia",
        "klon": "Klon",
        "elevenlabs": "ElevenLabs",
        "google": "Google Cloud",
        "local": "Lokales",
        "kokoro": "Kokoro",
        "piper": "Piper",
    }.get(engine, engine)


def _engine_is_paused(engine: str, *, ignore_error_cooldown: bool = False) -> bool:
    compat_name = _pause_until_compat_name(engine)
    legacy_deadline = globals().get(compat_name, 0.0) if compat_name else 0.0
    deadline = max(_engine_paused_until.get(engine, 0.0), legacy_deadline)
    now = time.monotonic()
    if now >= deadline:
        return False
    if not ignore_error_cooldown:
        return True
    if now < _engine_quota_paused_until.get(engine, 0.0):
        return True
    # Nur ein von uns eindeutig als kurz markierter Fehler-Cooldown ist
    # innerhalb eines bereits hörbaren Turns übersteuerbar.
    return deadline > _engine_error_paused_until.get(engine, 0.0)


def _cartesia_is_paused() -> bool:
    return _engine_is_paused("cartesia")


def _elevenlabs_is_paused() -> bool:
    return _engine_is_paused("elevenlabs")


def _google_tts_is_paused() -> bool:
    return _engine_is_paused("google")


def _start_tts_turn() -> tuple[TTSTurnState, contextvars.Token]:
    state = TTSTurnState()
    state.engine, state.chain_position = _first_available_engine()
    return state, _active_tts_turn.set(state)


def _end_tts_turn(token: contextvars.Token) -> None:
    _active_tts_turn.reset(token)


def _tts_turn_can_use_filler(state: TTSTurnState) -> bool:
    """Füller gibt es erst, wenn im aktuellen Turn die Hauptstimme feststeht."""
    chain = _engine_chain()
    return bool(
        chain
        and state.engine == chain[0]
        and state.chain_position == 0
        and _turn_engine_available(chain[0])
    )


def _kokoro_paths() -> tuple[Path, Path]:
    return _KOKORO_DIR / "kokoro-v1.0.onnx", _KOKORO_DIR / "voices-v1.0.bin"


def _load_kokoro():
    """Lädt Kokoro einmalig. Gibt None zurück (und schaltet dauerhaft auf Piper),
    wenn Paket, Modell oder espeak-ng fehlen. Nur unter _lock aufrufen."""
    global _kokoro, _kokoro_unavailable
    if _kokoro is not None:
        return _kokoro
    onnx, voices = _kokoro_paths()
    if not (onnx.exists() and voices.exists()):
        log.warning(f"Kokoro-Modell fehlt in {_KOKORO_DIR} – nutze Piper")
        _kokoro_unavailable = True
        return None
    try:
        import os
        os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = _ESPEAK_LIB
        os.environ["ESPEAK_DATA_PATH"] = _ESPEAK_DATA
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        EspeakWrapper.set_library(_ESPEAK_LIB)
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(onnx), str(voices))
        log.info(f"🔊 Kokoro TTS geladen ({config.TTS_KOKORO_VOICE})")
        return _kokoro
    except Exception as e:
        # Typisch: espeak-ng nicht installiert (brew install espeak-ng).
        log.warning(f"Kokoro nicht nutzbar ({e}) – nutze Piper")
        _kokoro_unavailable = True
        return None


def _synth_kokoro(text: str, speed: float) -> bytes:
    """Text → WAV-Bytes über Kokoro. Nur aufrufen, wenn _load_kokoro() geliefert hat."""
    import numpy as np
    audio, sr = _kokoro.create(text, voice=config.TTS_KOKORO_VOICE, speed=speed or 1.0, lang="de")
    pcm = (np.clip(np.asarray(audio).reshape(-1), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def resolve_voice_paths(voice_name: str) -> tuple[Path, Path]:
    """Löst einen Stimmen-Schlüssel (z.B. 'karlsson-low') zu (onnx_path, config_path) auf.

    Wirft KeyError für unbekannte Namen — ein falscher Stimmen-Name ist ein
    Programmfehler (z.B. Tippfehler im Setting), kein zur Laufzeit erwarteter Zustand.
    """
    filename = VOICE_MODELS[voice_name]
    onnx = _MODEL_DIR / f"{filename}.onnx"
    config = _MODEL_DIR / f"{filename}.onnx.json"
    return onnx, config


def _active_voice_name() -> str:
    return db.get_setting("tts_voice", DEFAULT_VOICE) or DEFAULT_VOICE


def is_available() -> bool:
    """True, wenn mindestens ein Provider der Ersatzkette einsatzbereit ist."""
    state = _active_tts_turn.get()
    if state is not None and _turn_bound_engine_available(state):
        return True
    return any(_engine_available(engine) for engine in _engine_chain())


def _local_engine_available(engine: str) -> bool:
    if engine in {"local", "kokoro"} and not _kokoro_unavailable:
        onnx, voices = _kokoro_paths()
        if onnx.exists() and voices.exists():
            return True
    try:
        onnx, voice_config = resolve_voice_paths(_active_voice_name())
    except KeyError:
        return False
    return onnx.exists() and voice_config.exists()


def _load_voice():
    global _voice, _loaded_voice_name
    voice_name = _active_voice_name()
    if _voice is not None and _loaded_voice_name == voice_name:
        return _voice
    onnx, config = resolve_voice_paths(voice_name)
    if not (onnx.exists() and config.exists()):
        raise RuntimeError(
            f"Piper-Modell '{voice_name}' nicht gefunden in {_MODEL_DIR}. "
            f"Bitte {onnx.name} (+ .onnx.json) herunterladen."
        )
    from piper import PiperVoice
    _voice = PiperVoice.load(str(onnx), str(config))
    _loaded_voice_name = voice_name
    log.info(f"🔊 Piper TTS geladen ({voice_name})")
    return _voice


def _clean_for_speech(text: str) -> str:
    """Markdown und Sonderzeichen entfernen die beim Vorlesen störend klingen."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)             # *italic*
    text = re.sub(r'`+(.+?)`+', r'\1', text)             # `code`
    text = re.sub(r'#+\s*', '', text)                    # ## Header
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)  # [link](url)
    text = re.sub(r'https?://\S+', 'Link', text)         # nackte URLs
    text = re.sub(r'•\s*', '', text)                     # Bullet points
    text = re.sub(r'-{2,}', '', text)                    # --- Trennlinien
    text = re.sub(r'\n{3,}', '\n\n', text)               # Mehrfach-Leerzeilen
    return text.strip()


_SMALL_NUMBERS = {
    0: "null", 1: "eins", 2: "zwei", 3: "drei", 4: "vier", 5: "fünf",
    6: "sechs", 7: "sieben", 8: "acht", 9: "neun", 10: "zehn",
    11: "elf", 12: "zwölf", 13: "dreizehn", 14: "vierzehn", 15: "fünfzehn",
    16: "sechzehn", 17: "siebzehn", 18: "achtzehn", 19: "neunzehn",
}
_TENS = {
    20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig",
    60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig",
}
_MONTH_NAMES = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November",
    12: "Dezember",
}
_MONTH_PATTERN = (
    r"Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember"
)


def _fallback_number_to_german_words(number: int) -> str:
    """Kleiner deutscher Zahlwort-Fallback für Fälle ohne num2words."""
    number = int(number)
    if number < 0:
        return f"minus {_fallback_number_to_german_words(-number)}"
    if number < 20:
        return _SMALL_NUMBERS[number]
    if number < 100:
        tens = number // 10 * 10
        ones = number % 10
        if not ones:
            return _TENS[tens]
        unit = "ein" if ones == 1 else _SMALL_NUMBERS[ones]
        return f"{unit}und{_TENS[tens]}"
    if number < 1000:
        hundreds = number // 100
        rest = number % 100
        prefix = "einhundert" if hundreds == 1 else f"{_fallback_number_to_german_words(hundreds)}hundert"
        return prefix + (_fallback_number_to_german_words(rest) if rest else "")
    if number < 1_000_000:
        thousands = number // 1000
        rest = number % 1000
        prefix = "eintausend" if thousands == 1 else f"{_fallback_number_to_german_words(thousands)}tausend"
        return prefix + (_fallback_number_to_german_words(rest) if rest else "")
    for scale, singular, plural in (
        (1_000_000_000, "Milliarde", "Milliarden"),
        (1_000_000, "Million", "Millionen"),
    ):
        if number >= scale:
            count, rest = divmod(number, scale)
            count_words = "eine" if count == 1 else _fallback_number_to_german_words(count)
            result = f"{count_words} {singular if count == 1 else plural}"
            return result + (
                f" {_fallback_number_to_german_words(rest)}" if rest else ""
            )
    return " ".join(_SMALL_NUMBERS[int(digit)] for digit in str(number))


def _number_to_german_words(number: int) -> str:
    try:
        if _num2words is not None:
            return str(_num2words(int(number), lang="de"))
    except (NotImplementedError, OverflowError, ValueError):
        pass
    return _fallback_number_to_german_words(int(number))


def _ordinal_to_german_words(number: int) -> str:
    try:
        if _num2words is not None:
            return str(_num2words(int(number), to="ordinal", lang="de"))
    except (NotImplementedError, OverflowError, ValueError):
        pass
    if number == 1:
        return "erste"
    if number == 3:
        return "dritte"
    if number == 7:
        return "siebte"
    if number == 8:
        return "achte"
    cardinal = _fallback_number_to_german_words(number)
    return cardinal + ("te" if number < 20 else "ste")


def _date_ordinal(number: int, before: str) -> str:
    ordinal = _ordinal_to_german_words(number)
    context = before.casefold()
    if re.search(
        r"(?:\bam\s+|\bvom\s+|\bzum\s+|\bbis\s+(?:zum\s+)?|"
        r"\bseit\s+dem\s+|\bauf\s+den\s+|\bfür\s+den\s+|\bden\s+)$",
        context,
    ):
        return ordinal + "n"
    if re.search(r"\bder\s+$", context):
        return ordinal
    return ordinal[:-1] + "er" if ordinal.endswith("e") else ordinal + "er"


def _decimal_to_german_words(value: str) -> str:
    whole, fraction = value.split(",", 1)
    decimals = " ".join(_number_to_german_words(int(digit)) for digit in fraction)
    return f"{_number_to_german_words(int(whole))} Komma {decimals}"


def _digit_sequence_to_german_words(digits: str) -> str:
    return " ".join(_number_to_german_words(int(digit)) for digit in digits)


def _numbers_to_german_words(text: str) -> str:
    """Schreibt Datumszahlen, Uhrzeiten, Dezimalzahlen, Prozent und Kardinalzahlen aus."""
    spoken = str(text or "")

    def version(match):
        parts = match.group("number").split(".")
        words = [
            _digit_sequence_to_german_words(part)
            if len(part) > 1 and part.startswith("0")
            else _number_to_german_words(int(part))
            for part in parts
        ]
        label = match.group("label") or ""
        if label in ("v", "V"):
            label = "Version "
        return f"{label}{' Punkt '.join(words)}"

    def version_unlabeled(match):
        return version(match)

    # Versionsnummern sind keine Datumsangaben oder Dezimalzahlen.
    # Ohne Label nur eindeutige Formen: vier Teile oder drei Teile mit einstelligem Ende
    # (12.10.26 bleibt ein Datum, 1.2.3 ist eine Version).
    spoken = re.sub(
        r"(?<![\w.])(?P<label>[vV](?=\d))?(?P<number>\d+\.\d+\.\d(?:\.\d+)*|\d+(?:\.\d+){3,})(?!\w|\.\d)",
        lambda m: version(m) if m.group("label") is not None else version_unlabeled(m),
        spoken,
    )
    spoken = re.sub(
        r"(?P<label>\bVersion\s+)(?P<number>\d+(?:\.\d+)+)(?!\w|\.\d)",
        version,
        spoken,
        flags=re.IGNORECASE,
    )

    def date_range(match):
        first, last, month, year = match.groups()
        first_number, last_number, month_number = int(first), int(last), int(month)
        if (
            not 1 <= first_number <= 31
            or not 1 <= last_number <= 31
            or month_number not in _MONTH_NAMES
        ):
            return match.group(0)
        start = _date_ordinal(first_number, spoken[:match.start()])
        end = _date_ordinal(last_number, "bis ")
        result = f"{start} bis {end} {_MONTH_NAMES[month_number]}"
        if year:
            result += f" {_number_to_german_words(int(year))}"
        return result

    spoken = re.sub(
        r"(?<!\d)(\d{1,2})\.\s*[–—-]\s*(\d{1,2})\.(\d{1,2})\.(?:(\d{4})(?!\d))?",
        date_range,
        spoken,
    )

    def date_transition(match):
        first, connector, last, month = match.groups()
        start = _date_ordinal(int(first), spoken[:match.start()])
        end = _date_ordinal(int(last), f"{connector} ")
        return f"{start} {connector} {end} {month}"

    spoken = re.sub(
        rf"(?<!\d)(\d{{1,2}})\.\s+(auf\s+den)\s+(\d{{1,2}})\.\s*({_MONTH_PATTERN})\b",
        date_transition,
        spoken,
        flags=re.IGNORECASE,
    )

    def numeric_date(match):
        day, month, year = match.groups()
        day_number, month_number = int(day), int(month)
        if not 1 <= day_number <= 31 or month_number not in _MONTH_NAMES:
            return match.group(0)
        month_name = _MONTH_NAMES[month_number]
        date = f"{_date_ordinal(day_number, spoken[:match.start()])} {month_name}"
        if year:
            # Zweistellige Jahre (12.10.26) als 20xx lesen.
            year_number = int(year) + (2000 if len(year) == 2 else 0)
            date += f" {_number_to_german_words(year_number)}"
        return date

    spoken = re.sub(
        r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(?:(\d{4}|\d{2})(?!\d))?",
        numeric_date,
        spoken,
    )

    def named_date(match):
        day, month = match.groups()
        return f"{_date_ordinal(int(day), spoken[:match.start()])} {month}"

    spoken = re.sub(
        rf"(?<!\d)(\d{{1,2}})\.\s*({_MONTH_PATTERN})\b",
        named_date,
        spoken,
        flags=re.IGNORECASE,
    )

    def ordinal_before_noun(match):
        before = spoken[:match.start()]
        context = before.casefold()
        ordinal = _ordinal_to_german_words(int(match.group("number")))
        if re.search(r"(?:\b(?:im|am|beim|vom|zum|zur|ins)\s+|\b(?:den|dem|des)\s+)$", context):
            return f"{ordinal}n {match.group('noun')}"
        if re.search(r"\b(?:der|die|das)\s+$", context):
            return f"{ordinal} {match.group('noun')}"
        if re.search(r"(?:^|[.!?…])\s*$", before):
            strong = ordinal[:-1] + "er" if ordinal.endswith("e") else ordinal + "er"
            return f"{strong[0].upper() + strong[1:]} {match.group('noun')}"
        return match.group(0)

    # Ein Punkt macht nur mit eindeutigem Nomenkontext eine Ordinalzahl.
    spoken = re.sub(
        r"(?<![\w])(?P<number>[0-9]{1,2})\.(?!\d)\s+(?P<noun>[A-ZÄÖÜ][\w-]*)\b",
        ordinal_before_noun,
        spoken,
    )

    spoken = re.sub(
        r"(?<![\d:])24:00(?:\s*Uhr\b)?(?![\d:])",
        "vierundzwanzig Uhr",
        spoken,
        flags=re.IGNORECASE,
    )

    def clock_time(match):
        hour, minute = (int(match.group(1)), int(match.group(2)))
        hour_word = "ein" if hour == 1 else _number_to_german_words(hour)
        time_words = f"{hour_word} Uhr"
        if minute:
            time_words += f" {_number_to_german_words(minute)}"
        return time_words

    spoken = re.sub(
        r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?:\s*Uhr\b)?(?![\d:])",
        clock_time,
        spoken,
        flags=re.IGNORECASE,
    )

    def grouped_thousands(match):
        number = int(match.group("number").replace(".", ""))
        words = _number_to_german_words(number)
        if 1_000 <= number < 2_000 and words.startswith("eintausend"):
            words = words[3:]
        return f"minus {words}" if match.group("sign") == "-" else words

    spoken = re.sub(
        r"(?<![\w.])(?P<sign>-?)(?P<number>[1-9]\d{0,2}(?:\.\d{3})+)(?!\.\d|\d|\w)",
        grouped_thousands,
        spoken,
    )

    preserved_dotted_numbers = []

    def preserve_ambiguous_dotted_number(match):
        token = chr(0xE000 + len(preserved_dotted_numbers))
        preserved_dotted_numbers.append((token, match.group(0)))
        return token

    # Nicht erkannte Punktzahlen können Versionen oder fremdformatierte Dezimalzahlen sein.
    spoken = re.sub(
        r"(?<!\d)\d+(?:\.\d+)+(?!\d)",
        preserve_ambiguous_dotted_number,
        spoken,
    )

    def phone_number(match):
        groups = re.split(r"[ \t\u00a0]+", match.group(0).strip())
        if not groups[0].startswith("0") and sum(map(len, groups)) < 7:
            return match.group(0)
        return ", ".join(_digit_sequence_to_german_words(group) for group in groups)

    spoken = re.sub(
        r"(?<![\w.])(?<!\d,)\d+(?:[ \t\u00a0]+\d+)*(?!\w|,\d)",
        phone_number,
        spoken,
    )

    def kilograms(match):
        value = match.group("number")
        words = (
            _decimal_to_german_words(value)
            if "," in value
            else _number_to_german_words(int(value))
        )
        return f"{words} Kilo"

    spoken = re.sub(
        r"(?<!\w)(?P<number>-?\d+(?:,\d+)?)\s*kg\b",
        kilograms,
        spoken,
        flags=re.IGNORECASE,
    )

    def percent(match):
        number = match.group(1)
        words = _decimal_to_german_words(number) if "," in number else _number_to_german_words(int(number))
        return f"{words} Prozent"

    spoken = re.sub(r"(?<!\w)(-?\d+(?:,\d+)?)\s*%(?!\w)", percent, spoken)
    spoken = re.sub(
        r"(?<!\w)(-?\d+),(\d+)(?!\w)",
        lambda match: _decimal_to_german_words(match.group(0)),
        spoken,
    )
    spoken = re.sub(
        r"(?<![\w.])-(\d+)(?!\w)",
        lambda match: _number_to_german_words(-int(match.group(1))),
        spoken,
    )
    spoken = re.sub(
        r"(?<!\w)\d+(?!\w)",
        lambda match: _number_to_german_words(int(match.group(0))),
        spoken,
    )
    for token, original in preserved_dotted_numbers:
        spoken = spoken.replace(token, original)
    return spoken


def _synth(text: str, speed: float) -> bytes:
    """Synthesisiert Text → WAV-Bytes (läuft in Thread, blockiert nicht Event-Loop)."""
    from piper.config import SynthesisConfig
    voice = _load_voice()
    syn_config = SynthesisConfig(length_scale=1.0 / speed if speed else None)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue()


def _wav_to_ogg(wav_bytes: bytes) -> bytes:
    """WAV → OGG/Opus via ffmpeg (Telegram-kompatibles Format)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
        tmp_wav.write(wav_bytes)
        wav_path = tmp_wav.name

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_ogg:
        ogg_path = tmp_ogg.name

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", wav_path,
                "-c:a", "libopus",
                "-b:a", "64k",
                "-ar", "48000",
                ogg_path,
            ],
            capture_output=True,
            check=True,
        )
        return Path(ogg_path).read_bytes()
    finally:
        Path(wav_path).unlink(missing_ok=True)
        Path(ogg_path).unlink(missing_ok=True)


def _mp3_to_ogg(mp3_bytes: bytes) -> bytes:
    """MP3 → OGG/Opus via ffmpeg (Telegram-kompatibles Format)."""
    if not mp3_bytes:
        raise ValueError("ElevenLabs lieferte leeres MP3-Audio")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
        tmp_mp3.write(mp3_bytes)
        mp3_path = tmp_mp3.name

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_ogg:
        ogg_path = tmp_ogg.name

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", mp3_path,
                "-c:a", "libopus",
                "-b:a", "64k",
                "-ar", "48000",
                ogg_path,
            ],
            capture_output=True,
            check=True,
        )
        ogg = Path(ogg_path).read_bytes()
        if _ogg_opus_sample_count(ogg) <= 0:
            raise ValueError("ElevenLabs lieferte OGG ohne Samples")
        return ogg
    finally:
        Path(mp3_path).unlink(missing_ok=True)
        Path(ogg_path).unlink(missing_ok=True)


def _apply_cartesia_pronunciations(text: str) -> str:
    """Ersetzt Wörter, die die deutsche Cartesia-Stimme falsch ausspricht, durch eine Lautschreibung.

    Nur für die Sprachausgabe; der angezeigte Text bleibt unverändert.
    """
    for word, spoken in (config.TTS_CARTESIA_PRONUNCIATIONS or {}).items():
        text = re.sub(rf"\b{re.escape(word)}\b", spoken, text, flags=re.IGNORECASE)
    return text


def _normalized_provider_text(text: str) -> str:
    """Text in derselben normalisierten Form, die Cloud-TTS tatsächlich sendet."""
    return _apply_cartesia_pronunciations(_numbers_to_german_words(text))


def _apply_klon_pronunciations(text: str) -> str:
    """Schreibt Klon-Wörter phonetisch um, ohne längere Wörter anzutasten."""
    pronunciations = getattr(config, "TTS_KLON_PRONUNCIATIONS", {}) or {}
    for word, spoken in sorted(
        pronunciations.items(), key=lambda item: len(str(item[0])), reverse=True
    ):
        pattern = re.compile(rf"(?<!\w){re.escape(str(word))}(?!\w)", re.IGNORECASE)

        def preserve_case(match: re.Match, replacement=str(spoken)) -> str:
            if match.group(0).isupper():
                return replacement.upper()
            if match.group(0).istitle():
                return replacement[:1].upper() + replacement[1:].lower()
            return replacement.lower()

        text = pattern.sub(preserve_case, text)
    return text


def _normalized_klon_text(text: str) -> str:
    """Zahlen und Klon-spezifische Aussprache; Cartesia-Regeln bleiben getrennt."""
    return _apply_klon_pronunciations(_numbers_to_german_words(text))


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    if not pcm or len(pcm) % 2 or sample_rate <= 0:
        raise ValueError("Klon lieferte leeres oder ungültiges PCM")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


async def _klon_ogg_stream(text: str):
    """Konvertiert jeden frühen Klon-PCM-Chunk separat in abspielbares OGG/Opus."""
    normalized_text = _normalized_klon_text(text)
    async for pcm, sample_rate in _klon_pcm_stream(normalized_text):
        wav = _pcm_to_wav(pcm, sample_rate)
        ogg = await asyncio.to_thread(_wav_to_ogg, wav)
        if not ogg or _ogg_opus_sample_count(ogg) <= 0:
            raise ValueError("Klon-TTS lieferte OGG ohne Samples")
        yield ogg
    _record_klon_usage(normalized_text)


async def _cartesia_synthesize(text: str) -> bytes:
    """Ruft Cartesia auf und liefert WAV-Bytes für die vorhandene OGG-Konvertierung."""
    api_key = _cartesia_key()
    payload = {
        "model_id": config.TTS_CARTESIA_MODEL,
        "transcript": _normalized_provider_text(text),
        "voice": {"mode": "id", "id": config.TTS_CARTESIA_VOICE},
        "language": config.TTS_CARTESIA_LANGUAGE,
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Cartesia-Version": _CARTESIA_VERSION,
    }
    async with httpx.AsyncClient(timeout=_CARTESIA_TIMEOUT_S) as client:
        response = await client.post(_CARTESIA_URL, headers=headers, json=payload)
    response.raise_for_status()
    return response.content


async def _elevenlabs_synthesize(text: str) -> bytes:
    """Ruft ElevenLabs auf und liefert MP3-Audio für die Opus-Konvertierung."""
    voice_id = config.TTS_ELEVENLABS_VOICE
    payload = {
        "text": _normalized_provider_text(text),
        "model_id": config.TTS_ELEVENLABS_MODEL,
        "language_code": "de",
    }
    headers = {
        "xi-api-key": _elevenlabs_key(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_ELEVENLABS_TIMEOUT_S) as client:
        response = await client.post(
            f"{_ELEVENLABS_URL}/{voice_id}",
            headers=headers,
            params={"output_format": _ELEVENLABS_OUTPUT_FORMAT},
            json=payload,
        )
    response.raise_for_status()
    return response.content


async def _google_tts_request(text: str, audio_encoding: str) -> bytes:
    """Ruft Google Cloud TTS auf und dekodiert das Base64-Audio."""
    payload = {
        "input": {
            "text": _normalized_provider_text(text),
        },
        "voice": {
            "languageCode": "de-DE",
            "name": config.TTS_GOOGLE_VOICE,
        },
        "audioConfig": {
            "audioEncoding": audio_encoding,
            "sampleRateHertz": 24000,
        },
    }
    headers = {
        "x-goog-api-key": _google_tts_key(),
        "Content-Type": "application/json; charset=utf-8",
    }
    async with httpx.AsyncClient(timeout=_GOOGLE_TTS_TIMEOUT_S) as client:
        response = await client.post(_GOOGLE_TTS_URL, headers=headers, json=payload)
    response.raise_for_status()
    try:
        encoded = response.json()["audioContent"]
        return base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("Google TTS lieferte kein gültiges Audio") from exc


def _google_ogg_encoding_unsupported(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    try:
        error = exc.response.json().get("error", {})
    except (ValueError, AttributeError):
        return False
    message = str(error.get("message", "")).lower()
    return error.get("status") == "INVALID_ARGUMENT" and any(
        term in message for term in ("ogg_opus", "audio encoding", "audioencoding")
    )


async def _google_tts_synthesize(text: str) -> tuple[bytes, bool]:
    """Liefert (Audio, ist_wav); OGG_OPUS wird bei Format-Ablehnung erneut angefragt."""
    try:
        return await _google_tts_request(text, "OGG_OPUS"), False
    except httpx.HTTPStatusError as exc:
        if not _google_ogg_encoding_unsupported(exc):
            raise
    return await _google_tts_request(text, "LINEAR16"), True


def _record_cartesia_usage(text: str) -> None:
    chars = len(text)
    estimated_cost = chars * float(config.TTS_CARTESIA_USD_PER_MILLION_CHARS) / 1_000_000
    try:
        from core import llm_usage

        llm_usage.record(
            "cartesia",
            config.TTS_CARTESIA_MODEL,
            chars,
            0,
            purpose="tts",
            actual_cost=estimated_cost,
        )
    except Exception as exc:
        log.debug("Cartesia-Usage konnte nicht erfasst werden: %s", redact_error_text(exc))


def _record_elevenlabs_usage(text: str) -> None:
    chars = len(text)
    estimated_cost = chars * float(config.TTS_ELEVENLABS_USD_PER_MILLION_CHARS) / 1_000_000
    try:
        from core import llm_usage

        llm_usage.record(
            "elevenlabs",
            config.TTS_ELEVENLABS_MODEL,
            chars,
            0,
            purpose="tts",
            actual_cost=estimated_cost,
        )
    except Exception as exc:
        log.debug("ElevenLabs-Usage konnte nicht erfasst werden: %s", redact_error_text(exc))


def _record_google_tts_usage(text: str) -> None:
    chars = len(text)
    estimated_cost = chars * _GOOGLE_TTS_USD_PER_MILLION_CHARS / 1_000_000
    try:
        from core import llm_usage

        llm_usage.record(
            "google",
            _GOOGLE_TTS_MODEL,
            chars,
            0,
            purpose="tts",
            actual_cost=estimated_cost,
        )
    except Exception as exc:
        log.debug("Google-TTS-Usage konnte nicht erfasst werden: %s", redact_error_text(exc))


def _record_klon_usage(text: str) -> None:
    chars = len(text)
    try:
        from core import llm_usage

        llm_usage.record(
            "klon",
            _KLON_MODEL,
            chars,
            0,
            purpose="tts",
            actual_cost=0.0,
        )
    except Exception as exc:
        log.debug("Klon-Usage konnte nicht erfasst werden: %s", redact_error_text(exc))


def _is_quota_error(engine: str, exc: httpx.HTTPStatusError) -> bool:
    status = exc.response.status_code
    try:
        body = exc.response.text.lower()
    except Exception:
        body = ""
    has_quota_hint = any(hint in body for hint in _QUOTA_HINTS)
    google_quota_exhausted = engine == "google" and "resource_exhausted" in body
    return status in {402, 429} or google_quota_exhausted or (
        engine in {"cartesia", "elevenlabs"} and has_quota_hint
    )


def _pause_engine(engine: str, status: int | str, *, quota: bool) -> None:
    duration = (
        max(0.0, float(getattr(config, "TTS_QUOTA_COOLDOWN_S", 3600.0)))
        if quota
        else _ERROR_COOLDOWN_S
    )
    compat_name = _pause_until_compat_name(engine)
    previous_deadline = max(
        _engine_paused_until.get(engine, 0.0),
        globals().get(compat_name, 0.0) if compat_name else 0.0,
    )
    requested_deadline = time.monotonic() + duration
    pause_deadlines = _engine_quota_paused_until if quota else _engine_error_paused_until
    pause_deadlines[engine] = max(pause_deadlines.get(engine, 0.0), requested_deadline)
    deadline = max(previous_deadline, requested_deadline)
    _engine_paused_until[engine] = deadline
    if compat_name:
        globals()[compat_name] = deadline
    if engine not in _engine_pause_warned:
        log.warning(
            "%s TTS pausiert nach %s für %s s; probiere den nächsten Provider",
            _engine_label(engine),
            f"HTTP {status}" if isinstance(status, int) else status,
            int(duration),
        )
        _engine_pause_warned.add(engine)
        compat_warn_name = {
            "cartesia": "_cartesia_pause_warned",
            "elevenlabs": "_elevenlabs_pause_warned",
            "google": "_google_tts_pause_warned",
        }.get(engine)
        if compat_warn_name:
            globals()[compat_warn_name] = True


def _clear_error_cooldown(engine: str) -> None:
    """Hebt nur einen kurzen Fehler-Cooldown nach erfolgreicher Recovery auf."""
    error_deadline = _engine_error_paused_until.pop(engine, 0.0)
    if error_deadline <= 0:
        return

    compat_name = _pause_until_compat_name(engine)
    current_deadline = max(
        _engine_paused_until.get(engine, 0.0),
        globals().get(compat_name, 0.0) if compat_name else 0.0,
    )
    quota_deadline = _engine_quota_paused_until.get(engine, 0.0)
    # Erhalte eine längere, nicht als Fehler markierte Pause (insbesondere Quota).
    deadline = max(quota_deadline, current_deadline if current_deadline > error_deadline else 0.0)
    _engine_paused_until[engine] = deadline
    if compat_name:
        globals()[compat_name] = deadline


def _has_provider_key(engine: str) -> bool:
    return {
        "cartesia": _cartesia_key,
        "elevenlabs": _elevenlabs_key,
        "google": _google_tts_key,
    }.get(engine, lambda: True)() != ""


def _engine_available(engine: str) -> bool:
    if engine in {"cartesia", "elevenlabs", "google"}:
        return _has_provider_key(engine) and not _engine_is_paused(engine)
    if engine == "klon":
        return _klon_is_configured()
    if engine in {"local", "kokoro", "piper"}:
        return _local_engine_available(engine)
    return False


def _turn_engine_available(engine: str) -> bool:
    """Prüft Füller-Preselection ohne synchronen Datenbankzugriff."""
    if engine in {"cartesia", "elevenlabs", "google"}:
        return _engine_available(engine)
    if engine == "klon":
        return _klon_is_configured()
    if engine in {"local", "kokoro"} and not _kokoro_unavailable:
        onnx, voices = _kokoro_paths()
        if onnx.exists() and voices.exists():
            return True
    if engine in {"local", "kokoro", "piper"}:
        voice_names = set(VOICE_MODELS)
        if _loaded_voice_name:
            voice_names.add(_loaded_voice_name)
        for voice_name in voice_names:
            try:
                onnx, voice_config = resolve_voice_paths(voice_name)
            except KeyError:
                continue
            if onnx.exists() and voice_config.exists():
                return True
    return False


def _turn_bound_engine_available(state: TTSTurnState) -> bool:
    """Erlaubt einer hörbaren Turn-Bindung kurze Fehlerpausen zu übergehen."""
    if not state.audio_emitted or state.engine is None or state.chain_position is None:
        return False
    chain = _engine_chain()
    position = state.chain_position
    if position >= len(chain) or chain[position] != state.engine:
        return False
    if state.engine in {"cartesia", "elevenlabs", "google"}:
        return _has_provider_key(state.engine) and not _engine_is_paused(
            state.engine, ignore_error_cooldown=True
        )
    return _turn_engine_available(state.engine)


def _first_available_engine() -> tuple[str | None, int | None]:
    for position, engine in enumerate(_engine_chain()):
        if _turn_engine_available(engine):
            return engine, position
    return None, None


async def _synthesize_klon(text: str) -> bytes:
    normalized_text = _normalized_klon_text(text)
    pcm_chunks: list[bytes] = []
    sample_rate = None
    async for pcm, chunk_rate in _klon_pcm_stream(normalized_text):
        if sample_rate is not None and sample_rate != chunk_rate:
            raise ValueError("Klon änderte die Sample-Rate im Stream")
        sample_rate = chunk_rate
        pcm_chunks.append(pcm)
    if not pcm_chunks or sample_rate is None:
        raise ValueError("Klon lieferte kein Audio")
    wav = _pcm_to_wav(b"".join(pcm_chunks), sample_rate)
    if _wav_nframes(wav) <= 0:
        raise ValueError("Klon lieferte WAV ohne Samples")
    ogg = await asyncio.to_thread(_wav_to_ogg, wav)
    if not ogg or _ogg_opus_sample_count(ogg) <= 0:
        raise ValueError("Klon lieferte OGG ohne Samples")
    _record_klon_usage(normalized_text)
    return ogg


async def _synthesize_provider(
    engine: str,
    text: str,
    speed: float,
    *,
    ignore_error_cooldown: bool = False,
) -> bytes | None:
    if engine == "klon":
        if not _klon_is_configured():
            return None
        try:
            return await _synthesize_klon(text)
        except Exception as exc:
            log.warning(
                "Klon-TTS fehlgeschlagen (%s); probiere den nächsten Provider",
                redact_error_text(exc),
            )
            return None

    if engine in {"cartesia", "elevenlabs", "google"}:
        if not _has_provider_key(engine) or _engine_is_paused(
            engine, ignore_error_cooldown=ignore_error_cooldown
        ):
            return None
        try:
            if engine == "cartesia":
                source_audio = await _cartesia_synthesize(text)
            elif engine == "elevenlabs":
                source_audio = await _elevenlabs_synthesize(text)
            else:
                source_audio, is_wav = await _google_tts_synthesize(text)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            is_quota = _is_quota_error(engine, exc)
            _pause_engine(engine, status, quota=is_quota)
            if engine == "cartesia" and is_quota:
                chain = _engine_chain()
                if "cartesia" in chain:
                    position = chain.index("cartesia")
                    if chain[position + 1:position + 2] == ["klon"] and _klon_is_configured():
                        _schedule_klon_prewarm()
            log.warning("%s TTS HTTP %s; probiere den nächsten Provider", _engine_label(engine), status)
            return None
        except Exception as exc:
            _pause_engine(engine, type(exc).__name__, quota=False)
            log.warning("%s TTS fehlgeschlagen (%s); probiere den nächsten Provider", _engine_label(engine), type(exc).__name__)
            return None

        try:
            if engine == "cartesia":
                ogg = await asyncio.to_thread(_cartesia_wav_to_ogg, source_audio)
            elif engine == "elevenlabs":
                ogg = await asyncio.to_thread(_mp3_to_ogg, source_audio)
                if _ogg_opus_sample_count(ogg) <= 0:
                    raise ValueError("ElevenLabs lieferte OGG ohne Samples")
            else:
                ogg = await asyncio.to_thread(_wav_to_ogg, source_audio) if is_wav else source_audio
                if not is_wav and not ogg.startswith(b"OggS"):
                    raise ValueError("Google TTS lieferte kein gültiges OGG")
                if _ogg_opus_sample_count(ogg) <= 0:
                    raise ValueError("Google TTS lieferte Audio ohne Samples")
            if not ogg:
                raise ValueError("TTS lieferte leeres Audio")
        except Exception as exc:
            _pause_engine(engine, type(exc).__name__, quota=False)
            log.warning("%s-TTS-Audio nicht nutzbar (%s); probiere den nächsten Provider", _engine_label(engine), type(exc).__name__)
            return None

        if engine == "cartesia":
            _record_cartesia_usage(_normalized_provider_text(text))
        elif engine == "elevenlabs":
            _record_elevenlabs_usage(_normalized_provider_text(text))
        else:
            _record_google_tts_usage(_normalized_provider_text(text))
        return ogg

    if engine in {"local", "kokoro", "piper"}:
        try:
            if engine == "piper" and _engine() != "piper":
                wav = await asyncio.to_thread(_synth, text, speed)
            else:
                wav = await _synthesize_local(text, speed)
            ogg = await asyncio.to_thread(_wav_to_ogg, wav)
            return ogg or None
        except Exception as exc:
            log.warning("Lokales TTS fehlgeschlagen (%s)", type(exc).__name__)
    else:
        log.warning("Unbekannte TTS-Engine '%s' wird übersprungen", engine)
    return None


def _wav_nframes(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as wav_file:
        return wav_file.getnframes()


def _ogg_opus_sample_count(audio: bytes) -> int:
    """Ermittelt die decodierbare Dauer eines OGG/Opus-Streams in Samples."""
    offset = 0
    pre_skip = None
    final_granule = None
    while offset + 27 <= len(audio):
        if audio[offset:offset + 4] != b"OggS":
            return 0
        segment_count = audio[offset + 26]
        table_start = offset + 27
        body_start = table_start + segment_count
        if body_start > len(audio):
            return 0
        body_size = sum(audio[table_start:body_start])
        page_end = body_start + body_size
        if page_end > len(audio):
            return 0
        granule = int.from_bytes(audio[offset + 6:offset + 14], "little")
        if granule != 0xFFFFFFFFFFFFFFFF:
            final_granule = granule
        head = audio[body_start:page_end].find(b"OpusHead")
        if head >= 0 and body_start + head + 12 <= page_end:
            pre_skip = int.from_bytes(audio[body_start + head + 10:body_start + head + 12], "little")
        offset = page_end
    if pre_skip is None or final_granule is None:
        return 0
    return max(0, final_granule - pre_skip)


def _cartesia_wav_to_ogg(wav: bytes) -> bytes:
    """Prüft das Cartesia-WAV (parsebar, > 0 Samples) und konvertiert es."""
    if _wav_nframes(wav) <= 0:
        raise ValueError("WAV ohne Samples")
    return _wav_to_ogg(wav)


async def _synthesize_local(text: str, speed: float) -> bytes:
    """Lokaler Fallback: Kokoro lazy laden, bei dessen Nichtverfügbarkeit Piper nutzen."""
    if _engine() in {"kokoro", "cartesia", "elevenlabs", "google", "local"} and not _kokoro_unavailable:
        if await _await_native_worker(asyncio.to_thread(_load_kokoro)) is not None:
            return await _await_native_worker(asyncio.to_thread(_synth_kokoro, text, speed))
    return await _await_native_worker(asyncio.to_thread(_synth, text, speed))


async def _synthesize_unlocked(
    text: str,
    speed: float,
    *,
    state: TTSTurnState | None = None,
    engines: list[str] | None = None,
) -> bytes:
    """Synthesisiert mit der Ersatzkette und merkt sich den Provider im Turn."""
    state = state or _active_tts_turn.get() or TTSTurnState()
    chain = list(engines if engines is not None else _engine_chain())
    start_position = state.chain_position or 0
    chain = chain[start_position:]
    for offset, engine in enumerate(chain, start=start_position):
        retry_bound_engine = state.audio_emitted and state.engine == engine
        audio = await _synthesize_provider(
            engine,
            text,
            speed,
            ignore_error_cooldown=retry_bound_engine,
        )
        if audio and retry_bound_engine:
            _clear_error_cooldown(engine)
        if not audio and retry_bound_engine:
            quota_paused = time.monotonic() < _engine_quota_paused_until.get(engine, 0.0)
            if not quota_paused:
                try:
                    audio = await asyncio.wait_for(
                        _synthesize_provider(
                            engine, text, speed, ignore_error_cooldown=True
                        ),
                        timeout=max(0.01, float(_BOUND_ENGINE_RETRY_TIMEOUT_S)),
                    )
                except TimeoutError:
                    _pause_engine(engine, "RetryTimeout", quota=False)
                    log.warning(
                        "%s TTS-Retry nach bereits ausgegebenem Audio lief ins Timeout",
                        _engine_label(engine),
                    )
                if audio:
                    _clear_error_cooldown(engine)
            if not audio:
                state.chain_position = offset + 1
        if audio:
            previous_engine = state.engine
            if state.audio_emitted and previous_engine and previous_engine != engine:
                log.warning(
                    "Stimmwechsel im laufenden Turn: %s → %s",
                    _engine_label(previous_engine),
                    _engine_label(engine),
                )
            state.engine = engine
            state.chain_position = offset
            state.audio_emitted = True
            log.info("🔊 TTS: %s · %s Zeichen → %sKB OGG", engine, len(text), len(audio) // 1024)
            return audio
    return b""


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    max_chars: int = 2000,
) -> bytes:
    """Text → OGG/Opus-Bytes (Telegram voice message format).

    `voice` bleibt aus API-Kompatibilitätsgründen erhalten. Cartesia und Google
    nutzen die konfigurierte Stimme; beim lokalen Fallback gelten die vorhandenen Stimmen.
    Kürzt automatisch auf max_chars um Timeouts zu vermeiden.
    Gibt leere Bytes zurück wenn TTS nicht verfügbar.
    """
    if not is_available():
        log.warning("TTS nicht verfügbar — Provider, Klon-Worker oder lokale Modelle fehlen")
        return b""

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."

    if not text.strip():
        return b""

    async with _lock:
        return await _synthesize_unlocked(text, speed)


async def synthesize_filler(text: str, max_chars: int = 2000) -> bytes:
    """Synthesisiert einen Cache-Füller ohne den Antwort-Stream-Lock.

    Füller werden vollständig als einzelne Datei erzeugt und nicht an den Stream
    gesendet. Der Cache-Aufbau darf deshalb einen laufenden Antwort-Stream nicht
    aufhalten.
    """
    if not is_available():
        log.warning("TTS nicht verfügbar — Provider, Klon-Worker oder lokale Modelle fehlen")
        return b""

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."
    if not text.strip():
        return b""

    chain = _engine_chain()
    if not chain:
        return b""
    # Der Cache gehört zur Hauptstimme. Wenn sie ausfällt, wird kein Audio einer
    # Ersatzstimme unter ihrer Cache-Identität abgelegt.
    return await _synthesize_unlocked(
        text,
        DEFAULT_SPEED,
        state=TTSTurnState(),
        engines=chain[:1],
    )


# Schnittgrößen fürs Streaming. Der erste Block ist bewusst klein: er bestimmt,
# wann der erste Ton kommt — gemessen 0,92 s bei 90 Zeichen gegen 1,87 s bei 150. Spätere dürfen wachsen — aber nur begrenzt:
#
# Während Block i+1 berechnet wird, läuft Block i als Wiedergabe. Damit der Puffer
# nicht leerläuft, muss gelten: rechenzeit(i+1) < audiodauer(i). Bei RTF r heißt das
# laenge(i+1) < laenge(i) / r — bei Kokoro (r≈0,21) also Faktor 4,8. _STREAM_WACHSTUM
# bleibt mit 3 darunter, damit auch Piper (r≈0,35 → Faktor 2,9) nicht reißt... knapp,
# deshalb greift zusätzlich _STREAM_MIN: sehr kurze Blöcke werden aufgefüllt, statt
# den nächsten künstlich klein zu halten.
_STREAM_FIRST_MAX_CHARS = 90   # erster Block klein halten: er bestimmt die Wartezeit
_STREAM_REST_MAX_CHARS = 250
_STREAM_MIN_CHARS = 35      # Untergrenze: zu kurze Blöcke puffern zu wenig Wiedergabezeit
_STREAM_WACHSTUM = 3        # ein Block höchstens 3× so lang wie sein Vorgänger
_STREAM_FIRST_EARLY_MIN_CHARS = 25
_STREAM_EARLY_BOUNDARY_RE = re.compile(r"(?<!\d),(?!\d)|(?<!\d)[–—:](?!\d)")


def _first_early_stream_boundary(text: str) -> int | None:
    """Liefert eine frühe Satzteilgrenze; Zahlen, Uhrzeiten und Zahlenbereiche bleiben zusammen."""
    for match in _STREAM_EARLY_BOUNDARY_RE.finditer(text):
        if match.end() >= _STREAM_FIRST_EARLY_MIN_CHARS:
            return match.end()
    return None


def _split_for_streaming(text: str) -> list[str]:
    """Zerlegt Text in Stücke, die einzeln synthetisiert werden.

    Der erste Block darf nach mindestens 25 Zeichen an Komma, Gedankenstrich oder
    Doppelpunkt enden. Danach wird wie bisher satzweise geschnitten. An solchen
    Satzteilgrenzen klingt der Übergang natürlich; Zahlen, Uhrzeiten, Zahlenbereiche
    und Dezimalkommas bleiben ungeteilt.

    Ohne passende frühe Satzteilgrenze wird ein einzelner überlanger Satz nicht
    willkürlich zerlegt — lieber ein spätes Stück als ein hörbarer Schnitt.
    """
    saetze = [t for t in re.split(r"(?<=[.!?…])\s+", text.strip()) if t.strip()]
    if not saetze:
        return []
    erster_frueher_teil = None
    grenze = _first_early_stream_boundary(saetze[0])
    if grenze is not None and saetze[0][grenze:].strip():
        erster_frueher_teil = saetze[0][:grenze].strip()
        saetze[0] = saetze[0][grenze:].strip()

    stuecke: list[str] = [erster_frueher_teil] if erster_frueher_teil else []
    aktuell = ""
    for satz in saetze:
        if not stuecke:
            grenze = _STREAM_FIRST_MAX_CHARS
        else:
            # An den Vorgänger gekoppelt: verhindert den Sprung "kurzer Einstiegssatz,
            # dann langer Block", bei dem die Wiedergabe den Berechner überholt.
            grenze = max(_STREAM_MIN_CHARS,
                         min(_STREAM_REST_MAX_CHARS, _STREAM_WACHSTUM * len(stuecke[-1])))
        if not aktuell:
            aktuell = satz
        elif len(aktuell) < _STREAM_MIN_CHARS or len(aktuell) + 1 + len(satz) <= grenze:
            # Die Mindestlänge schlägt die Obergrenze: ein zu kurzer Block ("Ja.")
            # puffert zu wenig Wiedergabezeit für die Berechnung des nächsten —
            # dann reißt der Ton mitten in der Antwort. Lieber etwas später anfangen.
            aktuell = f"{aktuell} {satz}"
        else:
            stuecke.append(aktuell)
            aktuell = satz
    if aktuell:
        stuecke.append(aktuell)
    return stuecke


async def _iterate_async_until(iterator, deadline: float):
    """Liefert Stücke bis zu einem Gesamtzeitlimit und schließt den Quellstream."""
    try:
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                item = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except StopAsyncIteration:
                return
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def _synthesize_unlocked_stream(
    text: str,
    speed: float,
    state: TTSTurnState,
):
    """Verwendet denselben Fallback wie der Volltextpfad und streamt Klon-PCM früh."""
    chain = _engine_chain()
    start_position = state.chain_position or 0
    for position in range(start_position, len(chain)):
        engine = chain[position]
        retry_bound_engine = state.audio_emitted and state.engine == engine
        if engine == "klon":
            if not _klon_is_configured():
                if retry_bound_engine:
                    state.chain_position = position + 1
                continue
            attempts = 2 if retry_bound_engine else 1
            for attempt in range(attempts):
                emitted = False
                try:
                    source = _klon_ogg_stream(text).__aiter__()
                    if attempt:
                        deadline = (
                            asyncio.get_running_loop().time()
                            + max(0.01, float(_BOUND_ENGINE_RETRY_TIMEOUT_S))
                        )
                        chunks = _iterate_async_until(source, deadline)
                    else:
                        chunks = source
                    async for ogg in chunks:
                        if not ogg:
                            continue
                        emitted = True
                        previous_engine = state.engine
                        if state.audio_emitted and previous_engine and previous_engine != engine:
                            log.warning(
                                "Stimmwechsel im laufenden Turn: %s → %s",
                                _engine_label(previous_engine),
                                _engine_label(engine),
                            )
                        state.engine = engine
                        state.chain_position = position
                        state.audio_emitted = True
                        yield ogg
                    if emitted:
                        return
                except Exception as exc:
                    log.warning(
                        "Klon-TTS-Stream fehlgeschlagen (%s)", redact_error_text(exc),
                    )
                    if emitted:
                        state.chain_position = position + 1
                        raise
                    if attempt and isinstance(exc, TimeoutError):
                        _pause_engine(engine, "RetryTimeout", quota=False)
                        log.warning(
                            "%s TTS-Retry nach bereits ausgegebenem Audio lief ins Timeout",
                            _engine_label(engine),
                        )
                    if attempt == 0 and retry_bound_engine:
                        continue
                    break
            if retry_bound_engine:
                state.chain_position = position + 1
            continue

        audio = await _synthesize_provider(
            engine,
            text,
            speed,
            ignore_error_cooldown=retry_bound_engine,
        )
        if audio and retry_bound_engine:
            _clear_error_cooldown(engine)
        if not audio and retry_bound_engine:
            quota_paused = time.monotonic() < _engine_quota_paused_until.get(engine, 0.0)
            if not quota_paused:
                try:
                    audio = await asyncio.wait_for(
                        _synthesize_provider(engine, text, speed, ignore_error_cooldown=True),
                        timeout=max(0.01, float(_BOUND_ENGINE_RETRY_TIMEOUT_S)),
                    )
                except TimeoutError:
                    _pause_engine(engine, "RetryTimeout", quota=False)
                    log.warning(
                        "%s TTS-Retry nach bereits ausgegebenem Audio lief ins Timeout",
                        _engine_label(engine),
                    )
                if audio:
                    _clear_error_cooldown(engine)
        if not audio:
            if retry_bound_engine:
                state.chain_position = position + 1
            continue

        previous_engine = state.engine
        if state.audio_emitted and previous_engine and previous_engine != engine:
            log.warning(
                "Stimmwechsel im laufenden Turn: %s → %s",
                _engine_label(previous_engine),
                _engine_label(engine),
            )
        state.engine = engine
        state.chain_position = position
        state.audio_emitted = True
        yield audio
        return


async def synthesize_stream(
    text: str,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    max_chars: int = 2000,
) -> AsyncIterator[bytes]:
    """Wie synthesize(), liefert die Antwort aber stückweise als einzelne
    OGG/Opus-Blöcke — jeder für sich abspielbar.

    Damit startet die Wiedergabe nach dem ersten Satzteil oder Satz statt nach der
    ganzen Antwort: bei vier Sätzen rund 0,6 s statt 4,5 s bis zum ersten Ton.

    Der Lock wird über den GESAMTEN Stream gehalten. Zwei gleichzeitige Antworten
    würden sonst ihre Stücke ineinander schieben — und es läuft ohnehin nur ein
    TTS-Modell.
    """
    if not is_available():
        log.warning("TTS nicht verfügbar — weder Klon-Worker noch Kokoro/Piper einsatzbereit")
        return

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."
    if not text.strip():
        return

    stuecke = _split_for_streaming(text)
    turn_state = _active_tts_turn.get() or TTSTurnState()
    completed_parts: list[str] = []
    async with _lock:
        for i, stueck in enumerate(stuecke):
            emitted_in_piece = False
            try:
                async for ogg in _synthesize_unlocked_stream(stueck, speed, turn_state):
                    if not ogg:
                        continue
                    emitted_in_piece = True
                    log.info(
                        "🔊 TTS-Stück %s/%s: %s Zeichen → %sKB OGG",
                        i + 1,
                        len(stuecke),
                        len(stueck),
                        len(ogg) // 1024,
                    )
                    yield ogg
                if not emitted_in_piece:
                    raise RuntimeError("TTS-Textteil lieferte kein Audio")
            except Exception as e:
                log.error("TTS-Stück %s fehlgeschlagen: %s", i, redact_error_text(e))
                raise TTSSynthesisIncompleteError(
                    f"TTS-Stück {i + 1} fehlgeschlagen: {redact_error_text(e)}",
                    synthesized_text=" ".join(completed_parts),
                ) from e
            completed_parts.append(stueck)


async def list_voices() -> list[str]:
    """Gibt die verfügbaren Stimmen zurück."""
    if not is_available():
        return []
    engine = (_engine_chain() or [_engine()])[0]
    if engine == "cartesia":
        return [config.TTS_CARTESIA_VOICE]
    if engine == "klon":
        return ["nico"]
    if engine == "elevenlabs":
        return [config.TTS_ELEVENLABS_VOICE]
    if engine == "google":
        return [config.TTS_GOOGLE_VOICE]
    if engine in {"local", "kokoro"} and not _kokoro_unavailable:
        return [config.TTS_KOKORO_VOICE]
    return [DEFAULT_VOICE]
