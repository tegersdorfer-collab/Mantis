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
import logging
import time

import config
from core import decisions, fast

log = logging.getLogger(__name__)

_whisper_model = None
_parakeet_model = None
# Beide Engines teilen sich EINEN Lock: whisper.cpp/ggml crasht bei parallelen
# transcribe()-Aufrufen (s.u.), und zwei gleichzeitig geladene STT-Modelle wären auf
# dem 16-GB-Mac ohnehin verschwenderisch.
_whisper_lock = asyncio.Lock()
# Wird auf True gesetzt, sobald Parakeet nachweislich nicht verfügbar ist (fehlendes
# Paket oder fehlgeschlagener Modell-Download). Dann geht es dauerhaft über whisper,
# statt bei jeder Äußerung erneut in denselben Fehler zu laufen.
_parakeet_unavailable = False

# Nach jeder Mantis-Antwort bleibt für dieses Fenster jede Folge-Äußerung automatisch
# "adressiert" — ohne das würde is_addressed_to_mantis() kurze Antworten wie "ja",
# "zeig mir das" oder "und morgen?" (kein Name, kein eindeutiger Befehl) ignorieren,
# weil jede Äußerung isoliert bewertet wird statt als Teil eines laufenden Gesprächs.
CONVERSATION_FOLLOWUP_WINDOW_S = 15
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
                    return text
        log.warning("Parakeet nicht verfügbar – ab jetzt whisper.cpp")

    return await _transcribe_whisper(audio_path)


async def _transcribe_parakeet(audio_path: str) -> str:
    """Parakeet-TDT über MLX. Setzt bei fehlendem Paket/Modell _parakeet_unavailable,
    damit der Aufrufer einmalig auf whisper umschaltet. Nur unter _whisper_lock aufrufen."""
    global _parakeet_model, _parakeet_unavailable
    if _parakeet_model is None:
        try:
            from parakeet_mlx import from_pretrained
        except ImportError:
            log.warning("parakeet-mlx nicht installiert")
            _parakeet_unavailable = True
            return ""
        model_id = getattr(config, "STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
        log.info(f"🔊 Lade Parakeet-Modell '{model_id}' …")
        try:
            _parakeet_model = await asyncio.to_thread(from_pretrained, model_id)
        except Exception as e:
            # Typisch: kein Netz beim allerersten Start (Modell noch nicht im HF-Cache).
            log.error(f"Parakeet-Modell konnte nicht geladen werden: {e}")
            _parakeet_unavailable = True
            return ""

    try:
        result = await asyncio.to_thread(_parakeet_model.transcribe, audio_path)
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
    adressiert, auch ohne Namen oder klaren Befehl. (3) Sonst entscheidet ein
    kleines dediziertes Modell (core.fast mit ADDRESS_CHECK_MODEL statt dem großen
    AGENT_MODEL_FAST), ob es sich auch ohne Namensnennung um eine an Mantis
    gerichtete Anfrage handelt (Spec: "nicht nur Wake-Word"). Seit 18.09.2026 fragt
    Layer 3 zuerst Jev (core/decisions.addressed, Benchmark 14/14 statt 88 %); das
    lokale Modell ist Fallback bei Ausfall oder Unsicherheit."""
    stripped = text.strip()
    if not stripped:
        return False
    if "mantis" in stripped.lower():
        return True
    if time.monotonic() < _conversation_active_until:
        return True

    async def _lokal() -> bool:
        return await fast.yes_no(
            f"Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten "
            f"namens Mantis (nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name "
            f"'Mantis' nicht genannt wird?\n\n\"{stripped}\"",
            model=config.ADDRESS_CHECK_MODEL,
        )
    return await decisions.addressed(stripped, _lokal)
