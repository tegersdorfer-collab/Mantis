"""
TTS — Text-to-Speech, lokal. Seit 22.09.2026 Kokoro (deutsche Stimme "Martin"),
Piper bleibt als Fallback.

Gibt OGG/Opus-Bytes zurück die direkt als Telegram-Sprachnachricht verschickt
werden können. Lazy-loaded beim ersten Aufruf, dann im RAM gecacht.

**Kokoro kommt damit zurück** — es war früher ausgeschieden, weil es kein Deutsch
konnte (nur en/ja/zh/es/fr/hi/it/pt). Inzwischen gibt es deutsche Finetunes; dieser
hier bringt die männliche Stimme "Martin" mit, also dieselbe Rolle wie Piper-Thorsten.

Modellvergleich 22.09.2026 (voller Report: bench/TTS-2026-09-22.md), gemessen als
Streaming: Zeit bis zum ERSTEN Ton, nicht Gesamtlatenz — das ist, was man hört.

    Kokoro Martin      0,52 s   RTF 0,21   0,62 GB   Verständlichkeit 3,3 %
    Piper Thorsten     1,06 s   RTF 0,35   0,03 GB                    1,6 %
    Qwen3-TTS (Klon)   0,64 s   RTF 0,59   2,40 GB                    8,9 %
    Kartoffelbox-Turbo 2,85 s   RTF 1,20   2,50 GB                    1,7 %
    Chatterbox ML      4,76 s   RTF 1,46   3,23 GB                    0,0 %

Entscheidend ist RTF < 1: darüber generiert das Modell langsamer als die Wiedergabe,
der Puffer läuft leer und es stottert — daran scheitern alle autoregressiven Modelle
(Chatterbox, Orpheus, Kartoffelbox). Kokoro ist wie Piper nicht-autoregressiv und
erzeugt die Sequenz in einem Durchlauf; daher der Geschwindigkeitsvorsprung trotz
neuronaler Prosodie.

**Braucht espeak-ng** für die deutsche Phonemisierung (`brew install espeak-ng`).
Fehlt es, fällt die Synthese automatisch auf Piper zurück.
"""
from __future__ import annotations
import asyncio
import io
import logging
import re
import subprocess
import tempfile
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import config
from core import db

log = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).parent.parent / "data" / "tts" / "piper"
_KOKORO_DIR = Path(__file__).parent.parent / "data" / "tts" / "kokoro"
_ESPEAK_LIB = "/opt/homebrew/lib/libespeak-ng.dylib"
_ESPEAK_DATA = "/opt/homebrew/share/espeak-ng-data"

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
_lock = asyncio.Lock()


def _engine() -> str:
    return getattr(config, "TTS_ENGINE", "kokoro")


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
    """True, wenn IRGENDEINE Engine sprechen kann — Kokoro oder Piper."""
    if _engine() == "kokoro" and not _kokoro_unavailable:
        onnx, voices = _kokoro_paths()
        if onnx.exists() and voices.exists():
            return True
    try:
        onnx, cfg = resolve_voice_paths(_active_voice_name())
    except KeyError:
        return False
    return onnx.exists() and cfg.exists()


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


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    max_chars: int = 2000,
) -> bytes:
    """Text → OGG/Opus-Bytes (Telegram voice message format).

    `voice` bleibt aus API-Kompatibilitätsgründen erhalten, wird aktuell aber
    ignoriert — es ist nur eine deutsche Piper-Stimme geladen.
    Kürzt automatisch auf max_chars um Timeouts zu vermeiden.
    Gibt leere Bytes zurück wenn TTS nicht verfügbar.
    """
    if not is_available():
        log.warning("TTS nicht verfügbar — Modelle fehlen in data/tts/piper/")
        return b""

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."

    if not text.strip():
        return b""

    async with _lock:
        try:
            wav = None
            if _engine() == "kokoro" and not _kokoro_unavailable:
                if await asyncio.to_thread(_load_kokoro) is not None:
                    wav = await asyncio.to_thread(_synth_kokoro, text, speed)
            if wav is None:
                wav = await asyncio.to_thread(_synth, text, speed)
            ogg = await asyncio.to_thread(_wav_to_ogg, wav)
            log.info(f"🔊 TTS: {len(text)} Zeichen → {len(ogg)//1024}KB OGG")
            return ogg
        except Exception as e:
            log.error(f"TTS fehlgeschlagen: {e}")
            return b""


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


def _split_for_streaming(text: str) -> list[str]:
    """Zerlegt Text in Stücke, die einzeln synthetisiert werden.

    Geschnitten wird NUR an Satzgrenzen. Das ist der Grund, warum die Stücke
    lückenlos aneinandergehängt klingen: an einem Satzende steht ohnehin eine
    Sprechpause, der Übergang fällt dort nicht auf. Innerhalb eines Satzes zu
    schneiden würde man hören.

    Ein einzelner überlanger Satz wird nicht zerlegt — lieber ein spätes Stück
    als ein hörbarer Schnitt mitten im Satz.
    """
    saetze = [t for t in re.split(r"(?<=[.!?…])\s+", text.strip()) if t.strip()]
    if not saetze:
        return []
    stuecke: list[str] = []
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


async def synthesize_stream(
    text: str,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    max_chars: int = 2000,
) -> AsyncIterator[bytes]:
    """Wie synthesize(), liefert die Antwort aber stückweise als einzelne
    OGG/Opus-Blöcke — jeder für sich abspielbar.

    Damit startet die Wiedergabe nach dem ersten Satz statt nach der ganzen
    Antwort: bei vier Sätzen rund 0,6 s statt 4,5 s bis zum ersten Ton.

    Der Lock wird über den GESAMTEN Stream gehalten. Zwei gleichzeitige Antworten
    würden sonst ihre Stücke ineinander schieben — und es läuft ohnehin nur ein
    TTS-Modell.
    """
    if not is_available():
        log.warning("TTS nicht verfügbar — weder Kokoro noch Piper einsatzbereit")
        return

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."
    if not text.strip():
        return

    stuecke = _split_for_streaming(text)
    async with _lock:
        nutze_kokoro = False
        if _engine() == "kokoro" and not _kokoro_unavailable:
            nutze_kokoro = await asyncio.to_thread(_load_kokoro) is not None
        for i, stueck in enumerate(stuecke):
            try:
                wav = await asyncio.to_thread(
                    _synth_kokoro if nutze_kokoro else _synth, stueck, speed)
                ogg = await asyncio.to_thread(_wav_to_ogg, wav)
            except Exception as e:
                # Ein kaputtes Stück darf die restliche Antwort nicht verschlucken.
                log.error(f"TTS-Stück {i} fehlgeschlagen: {e}")
                continue
            log.info(f"🔊 TTS-Stück {i + 1}/{len(stuecke)}: {len(stueck)} Zeichen → {len(ogg) // 1024}KB")
            yield ogg


async def list_voices() -> list[str]:
    """Gibt die verfügbaren Stimmen zurück."""
    if not is_available():
        return []
    if _engine() == "kokoro" and not _kokoro_unavailable:
        return [config.TTS_KOKORO_VOICE]
    return [DEFAULT_VOICE]
