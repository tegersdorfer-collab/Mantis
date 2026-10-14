"""
Sprach-Verarbeitung — gemeinsame Transkription + schneller Adress-Check.

Seit 22.09.2026 läuft die Transkription standardmäßig über Parakeet-TDT-0.6b-v3
(NVIDIA, 25 europäische Sprachen) auf Apple MLX statt über whisper.cpp medium.
Ausschlaggebend war ein Benchmark auf den 36 ECHTEN Aufnahmen aus
data/wakeword/samples (Timos Stimme, echtes Mikrofon) — nicht die WER auf
synthetischem Audio, sondern die Frage, ob der Weckname ankommt:

    Parakeet v3      Weckname 72 %   0,17 s/Clip   2,7 GB Peak
    whisper medium   Weckname 28 %   0,78 s/Clip   1,9 GB Peak
    whisper turbo    Weckname 22 %   1,06 s/Clip   2,3 GB Peak

whisper machte aus "Mantis" reihenweise "Mentos", "Mentis", "Ventus". Ganze Sätze
transkribieren beide fehlerfrei — der Unterschied liegt bei kurzen Äußerungen und
Eigennamen, also genau im Voice-Alltag. Report: bench/STT-2026-09-22.md.

whisper.cpp bleibt als Fallback erhalten (STT_ENGINE="whisper", oder automatisch,
wenn parakeet-mlx nicht installiert ist).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import re
import time

import config
from core import decisions, fast

log = logging.getLogger(__name__)

_whisper_model = None
_parakeet_model = None
_parakeet_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet-mlx")
# Beide Engines teilen sich EINEN Lock: whisper.cpp/ggml crasht bei parallelen
# transcribe()-Aufrufen (s.u.), und zwei gleichzeitig geladene STT-Modelle wären auf
# dem 16-GB-Mac ohnehin verschwenderisch.
_whisper_lock = asyncio.Lock()
# Wird auf True gesetzt, sobald Parakeet nachweislich nicht verfügbar ist (fehlendes
# Paket oder fehlgeschlagener Modell-Download). Dann geht es dauerhaft über whisper,
# statt bei jeder Äußerung erneut in denselben Fehler zu laufen.
_parakeet_unavailable = False
_MANTIS_MISHEARINGS = frozenset({
    "mantasy", "bantas", "mantes", "mantiss", "mentis", "mandis",
    "mäntis", "mantus", "mandes",
})
_INITIAL_WORD_RE = re.compile(r"^(?P<prefix>\s*(?:hey\b[\s,!?-]*)?)(?P<word>[^\W_]+)", re.IGNORECASE)

# Nach jeder Mantis-Antwort bleibt für dieses Fenster jede Folge-Äußerung automatisch
# "adressiert" — ohne das würde is_addressed_to_mantis() kurze Antworten wie "ja",
# "zeig mir das" oder "und morgen?" (kein Name, kein eindeutiger Befehl) ignorieren,
# weil jede Äußerung isoliert bewertet wird statt als Teil eines laufenden Gesprächs.
CONVERSATION_FOLLOWUP_WINDOW_S = 15
VOICE_ADDRESS_TIMEOUT_S = 3.0
_conversation_active_until = 0.0


def mark_conversation_active() -> None:
    """Vom Voice-Router nach jeder erfolgreichen Mantis-Antwort aufzurufen."""
    global _conversation_active_until
    _conversation_active_until = time.monotonic() + CONVERSATION_FOLLOWUP_WINDOW_S


def _conversation_active() -> bool:
    return time.monotonic() < _conversation_active_until


async def transcribe_audio(audio_path: str) -> str:
    """Transkribiert eine Audiodatei lokal. Gibt leeren String bei Fehler zurück.

    Engine nach config.STT_ENGINE ("parakeet" oder "whisper"); ist Parakeet nicht
    verfügbar, übernimmt whisper.cpp dauerhaft."""
    global _parakeet_unavailable
    engine = getattr(config, "STT_ENGINE", "parakeet")

    if engine == "parakeet" and not _parakeet_unavailable:
        async with _whisper_lock:
            if not _parakeet_unavailable:  # kann sich gesetzt haben, während wir warteten
                text = await _transcribe_parakeet(audio_path)
                if not _parakeet_unavailable:
                    return _normalize_voice_transcript(text)
        log.warning("Parakeet nicht verfügbar – ab jetzt whisper.cpp")

    return _normalize_voice_transcript(await _transcribe_whisper(audio_path))


def _edit_distance_at_most_one(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 1:
        return 2
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        if min(current) > 1:
            return 2
        previous = current
    return previous[-1]


def _normalize_voice_transcript(text: str) -> str:
    """Korrigiert einen Mantis-ähnlichen Anredenamen am Anfang eines Voice-Transkripts."""
    match = _INITIAL_WORD_RE.match(text)
    if match is None:
        return text
    word = match.group("word")
    normalized = word.lower()
    if not 5 <= len(normalized) <= 8:
        return text
    if normalized not in _MANTIS_MISHEARINGS:
        if _edit_distance_at_most_one(normalized, "mantis") > 1:
            return text
    return f"{match.group('prefix')}Mantis{text[match.end('word'):]}"


def _load_parakeet_model(model_id: str):
    """Lädt Parakeet aus dem lokalen HF-Cache, sonst mit dem Paket-Downloadpfad."""
    from parakeet_mlx import from_pretrained

    try:
        from huggingface_hub import hf_hub_download

        config_path = hf_hub_download(model_id, "config.json", local_files_only=True)
        weights_path = hf_hub_download(model_id, "model.safetensors", local_files_only=True)
        if Path(config_path).parent == Path(weights_path).parent:
            # Eine lokale Snapshot-Directory verhindert, dass from_pretrained den Hub anfragt.
            return from_pretrained(str(Path(config_path).parent))
    except Exception:
        # Bei unvollständigem/fehlendem Cache übernimmt from_pretrained den Download.
        pass
    return from_pretrained(model_id)


async def preload_parakeet() -> None:
    """Lädt Parakeet im STT-Executor vor; Fehler bleiben ohne Einfluss auf den Start."""
    global _parakeet_model, _parakeet_unavailable
    if (
        not getattr(config, "STT_PRELOAD", True)
        or getattr(config, "STT_ENGINE", "parakeet") != "parakeet"
        or _parakeet_model is not None
        or _parakeet_unavailable
    ):
        return
    async with _whisper_lock:
        if _parakeet_model is not None or _parakeet_unavailable:
            return
        model_id = getattr(config, "STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
        try:
            loop = asyncio.get_running_loop()
            _parakeet_model = await loop.run_in_executor(
                _parakeet_executor, _load_parakeet_model, model_id,
            )
            log.info("Parakeet-Modell im Hintergrund vorgeladen")
        except Exception as exc:
            log.warning("Parakeet-Vorladen fehlgeschlagen (%s)", type(exc).__name__)


def schedule_parakeet_preload() -> asyncio.Task | None:
    """Plant das Modell-Laden als Hintergrund-Task ein, sofern es aktiviert ist."""
    if (
        not getattr(config, "STT_PRELOAD", True)
        or getattr(config, "STT_ENGINE", "parakeet") != "parakeet"
    ):
        return None
    return asyncio.create_task(preload_parakeet(), name="parakeet-stt-preload")


async def _transcribe_parakeet(audio_path: str) -> str:
    """Parakeet-TDT über MLX. Setzt bei fehlendem Paket/Modell _parakeet_unavailable,
    damit der Aufrufer einmalig auf whisper umschaltet. Nur unter _whisper_lock aufrufen."""
    global _parakeet_model, _parakeet_unavailable
    if _parakeet_model is None:
        model_id = getattr(config, "STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
        log.info(f"🔊 Lade Parakeet-Modell '{model_id}' …")
        try:
            loop = asyncio.get_running_loop()
            _parakeet_model = await loop.run_in_executor(
                _parakeet_executor, _load_parakeet_model, model_id,
            )
        except ImportError:
            log.warning("parakeet-mlx nicht installiert")
            _parakeet_unavailable = True
            return ""
        except Exception as e:
            # Typisch: kein Netz beim allerersten Start (Modell noch nicht im HF-Cache).
            log.error(f"Parakeet-Modell konnte nicht geladen werden: {e}")
            _parakeet_unavailable = True
            return ""

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_parakeet_executor, _parakeet_model.transcribe, audio_path)
        return (result.text or "").strip()
    except Exception as e:
        # Laufzeitfehler (kaputte Datei o.ä.) — kein Grund, die Engine zu wechseln.
        log.error(f"Parakeet-Transkription fehlgeschlagen: {e}")
        return ""


async def _transcribe_whisper(audio_path: str) -> str:
    """whisper.cpp medium — der Stand vor dem 22.09.2026, jetzt Fallback."""
    global _whisper_model
    try:
        from pywhispercpp.model import Model
    except ImportError:
        log.warning("pywhispercpp nicht installiert – Audio kann nicht transkribiert werden")
        return ""

    # whisper.cpp/ggml ist nicht thread-/reentrancy-sicher für parallele
    # transcribe()-Aufrufe auf demselben Modell-Kontext (concurrent Calls in den
    # Metal-Compute-Graph führen zu ggml_abort/SIGABRT, siehe Crash-Log vom
    # 2026-07-05 nach der VAD-Kalibrierungs-Änderung — die sensiblere Erkennung
    # löst häufiger überlappende Segmente aus, die vorher selten gleichzeitig
    # eintrafen). Modell-Laden UND Transkription laufen daher beide unter
    # demselben Lock, nicht nur das Laden.
    async with _whisper_lock:
        if _whisper_model is None:
            log.info("🔊 Lade whisper.cpp-Modell 'medium' …")
            _whisper_model = await asyncio.to_thread(
                Model, "medium", language="de", print_realtime=False, print_progress=False
            )

        try:
            segments = await asyncio.to_thread(_whisper_model.transcribe, audio_path)
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            log.error(f"Whisper-Transkription fehlgeschlagen: {e}")
            return ""


async def is_addressed_to_mantis(text: str) -> bool:
    """Schneller Ja/Nein-Check: ist dieser transkribierte Text ein an Mantis
    gerichteter Befehl/Anfrage? Leerer Text spart den LLM-Call.

    Drei Layer: (1) Keyword-Vorfilter — wird "Mantis" explizit genannt, sofort JA
    ohne LLM-Call (Latenz ~0). (2) Konversations-Fortsetzung — läuft gerade ein
    Gespräch (siehe mark_conversation_active()), gilt jede Folge-Äußerung als
    adressiert. (3) Sonst fragt der Check zuerst Jev und bei Unsicherheit die
    kurze Fast-Variante der Cloud-Kette; Fehler entscheiden konservativ mit NEIN."""
    stripped = text.strip()
    if not stripped:
        return False
    if "mantis" in stripped.lower():
        return True
    if time.monotonic() < _conversation_active_until:
        return True

    prompt = (
        f"Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten "
        f"namens Mantis (nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name "
        f"'Mantis' nicht genannt wird?\n\n\"{stripped}\""
    )
    try:
        async with asyncio.timeout(VOICE_ADDRESS_TIMEOUT_S):
            try:
                jev_answer = await decisions.voice_address(stripped)
            except Exception as e:
                log.debug("Jev-Voice-Adresscheck fehlgeschlagen: %s", e)
                jev_answer = None
            if jev_answer is not None:
                return jev_answer
            return await fast.yes_no(prompt)
    except Exception as e:
        log.debug("Voice-Adress-Check fehlgeschlagen: %s", e)
        return False
