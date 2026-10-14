"""Cartesia Sonic 3.6 als TTS mit lokalem Kokoro/Piper-Fallback.

Alle öffentlichen Funktionen liefern Telegram-kompatible OGG/Opus-Bytes. Cartesia
liefert WAV, das über dieselbe ffmpeg-Konvertierung wie der lokale Pfad umgewandelt
wird. Lokale Modelle werden erst geladen, wenn Cartesia nicht verfügbar ist.
"""
from __future__ import annotations
import asyncio
import io
import logging
import re
import subprocess
import tempfile
import time
import wave
from collections.abc import AsyncIterator
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
_CARTESIA_PAUSE_S = 30 * 60
_CARTESIA_PAUSE_STATUSES = {401, 402, 403, 429}

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
_lock = asyncio.Lock()


def _engine() -> str:
    return getattr(config, "TTS_ENGINE", "cartesia")


def _cartesia_key() -> str:
    return str(getattr(config, "CARTESIA_API_KEY", "") or "").strip()


def _cartesia_is_paused() -> bool:
    return time.monotonic() < _cartesia_paused_until


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
    if _engine() == "cartesia" and _cartesia_key() and not _cartesia_is_paused():
        return True
    if _engine() in {"kokoro", "cartesia"} and not _kokoro_unavailable:
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


def _apply_cartesia_pronunciations(text: str) -> str:
    """Ersetzt Wörter, die die deutsche Cartesia-Stimme falsch ausspricht, durch eine Lautschreibung.

    Nur für die Sprachausgabe; der angezeigte Text bleibt unverändert.
    """
    for word, spoken in (config.TTS_CARTESIA_PRONUNCIATIONS or {}).items():
        text = re.sub(rf"\b{re.escape(word)}\b", spoken, text, flags=re.IGNORECASE)
    return text


async def _cartesia_synthesize(text: str) -> bytes:
    """Ruft Cartesia auf und liefert WAV-Bytes für die vorhandene OGG-Konvertierung."""
    api_key = _cartesia_key()
    payload = {
        "model_id": config.TTS_CARTESIA_MODEL,
        "transcript": _apply_cartesia_pronunciations(_numbers_to_german_words(text)),
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


def _pause_cartesia(status: int) -> None:
    global _cartesia_paused_until, _cartesia_pause_warned
    _cartesia_paused_until = time.monotonic() + _CARTESIA_PAUSE_S
    if not _cartesia_pause_warned:
        log.warning(
            "Cartesia TTS pausiert nach HTTP %s für 30 Minuten; nutze lokales TTS",
            status,
        )
        _cartesia_pause_warned = True


async def _try_cartesia(text: str) -> bytes | None:
    """Gibt Cartesia-Audio oder None zurück, wenn der lokale Fallback übernehmen soll."""
    if not _cartesia_key() or _cartesia_is_paused():
        return None
    try:
        wav = await _cartesia_synthesize(text)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in _CARTESIA_PAUSE_STATUSES:
            _pause_cartesia(status)
        else:
            log.warning("Cartesia TTS HTTP %s; nutze lokales TTS", status)
        return None
    except Exception as exc:
        # Ausnahme-Texte von HTTP-Clients können Request-Details enthalten.
        log.warning("Cartesia TTS fehlgeschlagen (%s); nutze lokales TTS", type(exc).__name__)
        return None

    _record_cartesia_usage(text)
    return wav


def _wav_nframes(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as wav_file:
        return wav_file.getnframes()


def _cartesia_wav_to_ogg(wav: bytes) -> bytes:
    """Prüft das Cartesia-WAV (parsebar, > 0 Samples) und konvertiert es."""
    if _wav_nframes(wav) <= 0:
        raise ValueError("WAV ohne Samples")
    return _wav_to_ogg(wav)


async def _try_cartesia_ogg(text: str) -> bytes | None:
    """Cartesia-Audio als OGG oder None (Ausfall ODER nicht konvertierbares Audio).

    Kaputtes/leeres WAV bei HTTP 200 ist kein Auth-/Kontingentproblem: kein Pausieren,
    nur Warnung und lokaler Fallback für genau dieses Stück.
    """
    wav = await _try_cartesia(text)
    if wav is None:
        return None
    try:
        ogg = await asyncio.to_thread(_cartesia_wav_to_ogg, wav)
    except Exception as exc:
        log.warning("Cartesia-Audio nicht konvertierbar (%s); nutze lokales TTS", type(exc).__name__)
        return None
    if not ogg:
        log.warning("Cartesia-Audio ergab leeres OGG; nutze lokales TTS")
        return None
    return ogg


async def _synthesize_local(text: str, speed: float) -> bytes:
    """Lokaler Fallback: Kokoro lazy laden, bei dessen Nichtverfügbarkeit Piper nutzen."""
    if _engine() in {"kokoro", "cartesia"} and not _kokoro_unavailable:
        if await asyncio.to_thread(_load_kokoro) is not None:
            return await asyncio.to_thread(_synth_kokoro, text, speed)
    return await asyncio.to_thread(_synth, text, speed)


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    max_chars: int = 2000,
) -> bytes:
    """Text → OGG/Opus-Bytes (Telegram voice message format).

    `voice` bleibt aus API-Kompatibilitätsgründen erhalten. Cartesia nutzt die
    konfigurierte Stimme; beim lokalen Fallback gelten die vorhandenen Stimmen.
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
            ogg = await _try_cartesia_ogg(text) if _engine() == "cartesia" else None
            if ogg is None:
                wav = await _synthesize_local(text, speed)
                ogg = await asyncio.to_thread(_wav_to_ogg, wav)
            log.info(f"🔊 TTS: {len(text)} Zeichen → {len(ogg)//1024}KB OGG")
            return ogg
        except Exception as e:
            log.error("TTS fehlgeschlagen: %s", redact_error_text(e))
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
        log.warning("TTS nicht verfügbar — weder Kokoro noch Piper einsatzbereit")
        return

    text = _clean_for_speech(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(".", 1)[0] + "."
    if not text.strip():
        return

    stuecke = _split_for_streaming(text)
    async with _lock:
        for i, stueck in enumerate(stuecke):
            try:
                ogg = await _try_cartesia_ogg(stueck) if _engine() == "cartesia" else None
                if ogg is None:
                    wav = await _synthesize_local(stueck, speed)
                    ogg = await asyncio.to_thread(_wav_to_ogg, wav)
            except Exception as e:
                # Ein kaputtes Stück darf die restliche Antwort nicht verschlucken.
                log.error("TTS-Stück %s fehlgeschlagen: %s", i, redact_error_text(e))
                continue
            log.info(f"🔊 TTS-Stück {i + 1}/{len(stuecke)}: {len(stueck)} Zeichen → {len(ogg) // 1024}KB")
            yield ogg


async def list_voices() -> list[str]:
    """Gibt die verfügbaren Stimmen zurück."""
    if not is_available():
        return []
    if _engine() == "cartesia":
        return [config.TTS_CARTESIA_VOICE]
    if _engine() == "kokoro" and not _kokoro_unavailable:
        return [config.TTS_KOKORO_VOICE]
    return [DEFAULT_VOICE]
