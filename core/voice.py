"""
Sprach-Verarbeitung — gemeinsame Whisper-Transkription + schneller Adress-Check.

Nutzt whisper.cpp (via pywhispercpp) statt des reinen PyTorch-openai-whisper —
auf Apple Silicon läuft das über Metal statt nur CPU und ist damit deutlich
schneller bei gleicher Modellqualität (siehe docs/superpowers/plans, Phase 5a-
Nachbesserung). Whisper-Teil ist identisch zu dem, was communication/telegram.py
bisher exklusiv für Telegram-Sprachnachrichten nutzte — jetzt hier zentralisiert,
damit Phase 5 (Desktop-Sprachsteuerung) dieselbe Logik wiederverwendet statt sie
zu duplizieren.
"""
import asyncio
import logging
import time

import config
from core import decisions, fast

log = logging.getLogger(__name__)

_whisper_model = None
_whisper_lock = asyncio.Lock()

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
    """Transkribiert eine Audiodatei lokal mit whisper.cpp. Gibt leeren String bei Fehler zurück."""
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
