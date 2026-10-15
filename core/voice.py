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
import tempfile
import time
import wave

import config
from core import decisions, fast, stt_runtime

log = logging.getLogger(__name__)

_whisper_model = None
_parakeet_model = None
_parakeet_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet-mlx")
_parakeet_last_activity_at: float | None = None
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
    "mentiones", "mentions",
    "mäntis", "mantus", "mandes",
})
_VOICE_WAKEWORD_HINTS = frozenset({"mentiones", "mentions", "mantes", "mandis", "mentis"})
_INITIAL_WORD_RE = re.compile(
    r"^(?P<prefix>\s*(?:(?:hey|okay)\b[\s,!?-]*)?)(?P<word>[^\W_]+)",
    re.IGNORECASE,
)

# Nach jeder Mantis-Antwort bleiben Segmente für dieses Fenster aktiv. Die Voice-Route
# prüft normale Folgeäußerungen weiter auf Adressierung; lokale Reflexe dürfen direkt laufen.
CONVERSATION_FOLLOWUP_WINDOW_S = 15
VOICE_ADDRESS_TIMEOUT_S = 3.0


class VoiceConversationWindow:
    """Kurzlebiger Gesprächskontext, der genau einer Voice-Session gehört."""

    def __init__(self) -> None:
        self._active_until = 0.0
        self._last_response = ""

    def mark_active(self, response: str | None = None) -> None:
        self._active_until = time.monotonic() + CONVERSATION_FOLLOWUP_WINDOW_S
        if response and response.strip():
            self._last_response = response.strip()

    def is_active(self) -> bool:
        return time.monotonic() < self._active_until

    def address_context(self) -> str | None:
        if self.is_active() and self._last_response:
            return self._last_response
        return None


def _audio_duration_seconds(audio_path: str) -> float:
    """Liest die Dauer einer WAV-Datei, ohne ihren Audioinhalt einzulesen."""
    try:
        with wave.open(str(audio_path), "rb") as audio_file:
            sample_rate = audio_file.getframerate()
            if sample_rate > 0:
                return audio_file.getnframes() / sample_rate
    except (OSError, EOFError, wave.Error):
        pass
    return 0.0


def _record_stt_runtime(audio_path: str, started_at: float) -> None:
    elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000)
    stt_runtime.record_stt_runtime(_audio_duration_seconds(audio_path), elapsed_ms)


async def _await_worker_completion(awaitable):
    """Hält den STT-Lock, bis ein nativer Worker-Thread wirklich fertig ist.

    Das Abbrechen eines asyncio-Awaiters beendet einen bereits laufenden Thread
    nicht. Der Thread-Future bleibt deshalb abgeschirmt und wird vor dem
    Weiterreichen der Cancellation abgewartet.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Ein weiterer cancel() darf den Lock nicht freigeben, solange
                # der nicht abbrechbare Native-Worker noch auf dem Modell läuft.
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException:
            pass
        raise


def mark_conversation_active(
    conversation: VoiceConversationWindow | None = None,
    response: str | None = None,
) -> None:
    """Öffnet das Folgefenster nur für den übergebenen Voice-Kontext."""
    if conversation is not None:
        conversation.mark_active(response)


def _conversation_active(conversation: VoiceConversationWindow | None = None) -> bool:
    return conversation.is_active() if conversation is not None else False


async def transcribe_audio_with_hint(audio_path: str) -> tuple[str, bool]:
    """Liefert den normalisierten Text und einen nicht-freigebenden Namenshinweis."""
    global _parakeet_unavailable
    engine = getattr(config, "STT_ENGINE", "parakeet")

    if engine == "parakeet" and not _parakeet_unavailable:
        async with _whisper_lock:
            if not _parakeet_unavailable:  # kann sich gesetzt haben, während wir warteten
                raw_text = await _transcribe_parakeet(audio_path)
                if not _parakeet_unavailable:
                    return (
                        _normalize_voice_transcript(raw_text),
                        has_voice_wakeword_hint(raw_text),
                    )
        log.warning("Parakeet nicht verfügbar – ab jetzt whisper.cpp")

    raw_text = await _transcribe_whisper(audio_path)
    return _normalize_voice_transcript(raw_text), has_voice_wakeword_hint(raw_text)


async def transcribe_audio(audio_path: str) -> str:
    """Transkribiert eine Audiodatei lokal. Gibt leeren String bei Fehler zurück."""
    text, _wakeword_hint = await transcribe_audio_with_hint(audio_path)
    return text


def partial_stt_executor_available() -> bool:
    """Teil-STT darf nur starten, wenn weder finaler noch anderer STT-Lauf aktiv ist."""
    return not _whisper_lock.locked()


async def transcribe_audio_partial_with_hint(
    audio_path: str,
) -> tuple[str, bool] | None:
    """Teil-STT plus Weckworthinweis; bei belegtem STT-Pfad wird er verworfen."""
    global _parakeet_unavailable
    if _whisper_lock.locked():
        return None

    await _whisper_lock.acquire()
    try:
        engine = getattr(config, "STT_ENGINE", "parakeet")
        if engine == "parakeet" and not _parakeet_unavailable:
            raw_text = await _transcribe_parakeet(audio_path)
            if not _parakeet_unavailable:
                return (
                    _normalize_voice_transcript(raw_text),
                    has_voice_wakeword_hint(raw_text),
                )
        raw_text = await _transcribe_whisper_locked(audio_path)
        return _normalize_voice_transcript(raw_text), has_voice_wakeword_hint(raw_text)
    finally:
        _whisper_lock.release()


async def transcribe_audio_partial(audio_path: str) -> str | None:
    """Kompatible Text-only-Hülle für bestehende Teil-STT-Aufrufer."""
    result = await transcribe_audio_partial_with_hint(audio_path)
    return result[0] if result is not None else None


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
    if not 5 <= len(normalized) <= 9:
        return text
    if normalized not in _MANTIS_MISHEARINGS:
        if _edit_distance_at_most_one(normalized, "mantis") > 1:
            return text
    return f"{match.group('prefix')}Mantis{text[match.end('word'):]}"


def has_voice_wakeword_hint(text: str) -> bool:
    """Erkennt häufige STT-Fehlhörungen am Satzanfang als Hinweis, nie als Freigabe."""
    match = _INITIAL_WORD_RE.match(str(text or ""))
    return match is not None and match.group("word").casefold() in _VOICE_WAKEWORD_HINTS


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


def _warmup_parakeet(model) -> None:
    """Kompiliert den ersten MLX-Transkriptionspfad mit einer Sekunde Stille."""
    global _parakeet_last_activity_at
    wav_path = None
    warmup_pcm = b"\0\0" * 16_000
    try:
        with tempfile.NamedTemporaryFile(prefix="mantis-stt-warmup-", suffix=".wav", delete=False) as wav_file:
            wav_path = wav_file.name
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16_000)
            wav_file.writeframes(warmup_pcm)
        model.transcribe(wav_path)
        # Der Kompilier-/Aufwachlauf ist keine brauchbare Laufzeitschätzung für
        # normale Segmente. Er hält nur den MLX-Pfad warm.
        _parakeet_last_activity_at = time.monotonic()
    finally:
        if wav_path is not None:
            Path(wav_path).unlink(missing_ok=True)


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
            _parakeet_model = await _await_worker_completion(loop.run_in_executor(
                _parakeet_executor, _load_parakeet_model, model_id,
            ))
            try:
                await _await_worker_completion(loop.run_in_executor(
                    _parakeet_executor, _warmup_parakeet, _parakeet_model,
                ))
            except Exception as exc:
                log.warning("Parakeet-Warmup fehlgeschlagen (%s)", type(exc).__name__)
            log.info("Parakeet-Modell im Hintergrund vorgeladen")
        except Exception as exc:
            log.warning("Parakeet-Vorladen fehlgeschlagen (%s)", type(exc).__name__)


async def keepwarm_parakeet_once() -> bool:
    """Wärmt Parakeet nur auf, wenn der gemeinsame STT-Pfad gerade frei ist."""
    interval_s = max(0, int(getattr(config, "VOICE_STT_KEEPWARM_S", 240)))
    if (
        interval_s <= 0
        or getattr(config, "STT_ENGINE", "parakeet") != "parakeet"
        or _parakeet_model is None
        or _parakeet_unavailable
    ):
        return False
    if (
        _parakeet_last_activity_at is not None
        and time.monotonic() - _parakeet_last_activity_at < interval_s
    ):
        return False
    if _whisper_lock.locked():
        return False

    await _whisper_lock.acquire()
    try:
        if _parakeet_model is None or _parakeet_unavailable:
            return False
        loop = asyncio.get_running_loop()
        await _await_worker_completion(loop.run_in_executor(
            _parakeet_executor, _warmup_parakeet, _parakeet_model,
        ))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Parakeet-Keepwarm fehlgeschlagen (%s)", type(exc).__name__)
        return False
    finally:
        _whisper_lock.release()


async def _parakeet_keepwarm_loop(preload_task: asyncio.Task | None = None) -> None:
    if preload_task is not None:
        await asyncio.gather(preload_task, return_exceptions=True)
    interval_s = int(getattr(config, "VOICE_STT_KEEPWARM_S", 240))
    if interval_s <= 0:
        return
    while True:
        last_activity_at = _parakeet_last_activity_at
        next_warm_at = (
            time.monotonic() if last_activity_at is None else last_activity_at
        ) + interval_s
        delay_s = max(0.0, next_warm_at - time.monotonic())
        if delay_s <= 0:
            # The executor may still be occupied (for example by a native STT
            # call); back off instead of spinning when a warmup cannot start.
            delay_s = min(15.0, float(interval_s))
        await asyncio.sleep(delay_s)
        await keepwarm_parakeet_once()


def schedule_parakeet_keepwarm(
    preload_task: asyncio.Task | None = None,
) -> asyncio.Task | None:
    """Startet den periodischen Keepwarm-Task, wenn Parakeet aktiv ist."""
    if (
        int(getattr(config, "VOICE_STT_KEEPWARM_S", 240)) <= 0
        or getattr(config, "STT_ENGINE", "parakeet") != "parakeet"
    ):
        return None
    return asyncio.create_task(
        _parakeet_keepwarm_loop(preload_task), name="parakeet-stt-keepwarm",
    )


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
    global _parakeet_model, _parakeet_unavailable, _parakeet_last_activity_at
    if _parakeet_model is None:
        model_id = getattr(config, "STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
        log.info(f"🔊 Lade Parakeet-Modell '{model_id}' …")
        try:
            loop = asyncio.get_running_loop()
            _parakeet_model = await _await_worker_completion(loop.run_in_executor(
                _parakeet_executor, _load_parakeet_model, model_id,
            ))
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
        started_at = time.perf_counter()
        keepwarm_interval_s = int(getattr(config, "VOICE_STT_KEEPWARM_S", 240))
        cold_after_s = max(240, keepwarm_interval_s)
        was_cold = (
            _parakeet_last_activity_at is None
            or time.monotonic() - _parakeet_last_activity_at > cold_after_s
        )
        result = await _await_worker_completion(
            loop.run_in_executor(_parakeet_executor, _parakeet_model.transcribe, audio_path)
        )
        if not was_cold:
            _record_stt_runtime(audio_path, started_at)
        else:
            log.info("Parakeet nach Leerlauf aufgewacht; Laufzeit nicht fürs Partial-Budget gelernt")
        _parakeet_last_activity_at = time.monotonic()
        return (result.text or "").strip()
    except Exception as e:
        # Laufzeitfehler (kaputte Datei o.ä.) — kein Grund, die Engine zu wechseln.
        log.error(f"Parakeet-Transkription fehlgeschlagen: {e}")
        return ""


async def _transcribe_whisper(audio_path: str) -> str:
    """whisper.cpp medium — der Stand vor dem 22.09.2026, jetzt Fallback."""
    async with _whisper_lock:
        return await _transcribe_whisper_locked(audio_path)


async def _transcribe_whisper_locked(audio_path: str) -> str:
    """Whisper-Transkription bei bereits gehaltenem `_whisper_lock`."""
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
    if _whisper_model is None:
        log.info("🔊 Lade whisper.cpp-Modell 'medium' …")
        _whisper_model = await _await_worker_completion(asyncio.to_thread(
            Model, "medium", language="de", print_realtime=False, print_progress=False,
        ))

    try:
        started_at = time.perf_counter()
        segments = await _await_worker_completion(
            asyncio.to_thread(_whisper_model.transcribe, audio_path)
        )
        _record_stt_runtime(audio_path, started_at)
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        log.error(f"Whisper-Transkription fehlgeschlagen: {e}")
        return ""


async def is_addressed_to_mantis(
    text: str, *, conversation_context: str | None = None,
    wakeword_hint: bool = False,
) -> bool:
    """Schneller Ja/Nein-Check: ist dieser transkribierte Text ein an Mantis
    gerichteter Befehl/Anfrage? Leerer Text spart den LLM-Call.

    Ein akustischer openWakeWord-Treffer wird vom Aufrufer freigegeben. Ein Name im
    Transkript bleibt ein Hinweis und ersetzt den Adress-Check nicht."""
    stripped = text.strip()
    if not stripped:
        return False
    wakeword_hint = bool(wakeword_hint or has_voice_wakeword_hint(stripped))
    context = str(conversation_context or "").strip()[-1000:]
    prompt = (
        f"Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten "
        f"namens Mantis (nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name "
        f"'Mantis' nicht genannt wird?\n\n\"{stripped}\""
    )
    if context:
        prompt = (
            "Prüfe nur, ob die neue Spracheingabe an Mantis gerichtet ist. Die letzte Antwort "
            "von Mantis dient als Gesprächskontext und macht Raumgespräche nicht automatisch "
            f"zu Befehlen.\n\nLetzte Mantis-Antwort: \"{context}\"\n"
            f"Neue Spracheingabe: \"{stripped}\""
        )
    if wakeword_hint:
        prompt = (
            "Das Transkript beginnt möglicherweise mit einer STT-Fehlhörung des Weckworts. "
            "Das ist nur ein Hinweis, kein Freigabegrund; prüfe weiterhin die Absicht.\n\n"
            + prompt
        )
    try:
        async with asyncio.timeout(VOICE_ADDRESS_TIMEOUT_S):
            try:
                address_kwargs = {}
                if context:
                    address_kwargs["conversation_context"] = context
                if wakeword_hint:
                    address_kwargs["wakeword_hint"] = True
                jev_answer = await decisions.voice_address(stripped, **address_kwargs)
            except Exception as e:
                log.debug("Jev-Voice-Adresscheck fehlgeschlagen: %s", e)
                jev_answer = None
            if jev_answer is not None:
                return jev_answer
            return await fast.yes_no(prompt)
    except Exception as e:
        log.debug("Voice-Adress-Check fehlgeschlagen: %s", e)
        return False
