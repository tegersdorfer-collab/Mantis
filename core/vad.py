"""Silero VAD — Sprachaktivitätserkennung server-seitig, ersetzt die frühere
RMS-Schwelle in apps/desktop/src/voice-capture.ts (siehe
docs/superpowers/specs/2026-07-05-vad-wakeword-streaming-design.md)."""
from __future__ import annotations

import struct
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
import config

SAMPLE_RATE = 16000
MIN_SEGMENT_MS = 300
PREROLL_MS = 400

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_INCOMPLETE_ENDINGS = frozenset({
    "und", "oder", "aber", "dass", "weil", "ob", "wenn", "obwohl",
    "falls", "während", "sobald", "damit", "denn", "ähm", "hm", "also",
    "an", "auf", "aus", "bei", "durch", "für", "gegen", "hinter", "in",
    "neben", "ohne", "über", "um", "unter", "von", "vor", "zwischen",
    "mit", "nach", "zu", "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einen", "einem", "einer", "eines", "mein", "meine",
    "meinen", "meinem", "meiner", "meines", "dein", "deine", "deinen",
    "deinem", "deiner", "deines", "unser", "unsere", "unseren", "unserem",
    "unserer", "unseres", "ich", "du", "er", "sie", "es", "wir", "ihr",
    "mich", "mir", "dich", "dir", "ihn", "ihm", "uns", "euch", "viel",
    "wieviel", "gerne", "gern", "danach", "vielleicht",
})
_SUBORDINATORS = frozenset({
    "dass", "weil", "ob", "wenn", "obwohl", "falls", "während", "sobald",
    "damit",
})
_QUESTION_WORDS = frozenset({
    "wie", "was", "wer", "wen", "wem", "wessen", "wo", "wohin", "woher",
    "wann", "warum", "wieso", "weshalb", "welche", "welcher", "welches",
})
_QUESTION_PRONOUNS = frozenset({
    "ich", "du", "er", "sie", "es", "wir", "ihr", "mich", "mir", "dich",
    "dir", "ihn", "ihm", "uns", "euch", "das",
})
_QUESTION_VERBS = frozenset({
    "bin", "bist", "ist", "sind", "seid", "war", "waren", "wird", "werden",
    "habe", "hast", "hat", "haben", "hatte", "steht", "stehen", "gibt",
    "geht", "gehts", "kommt", "kostet", "macht", "kann", "kannst", "können",
    "soll", "sollst", "liege", "liegt", "schläft", "schlaf", "meinst",
})
_SEPARABLE_COMMANDS = frozenset({
    "mach", "mache", "schalt", "schalte", "dreh", "drehe", "stell", "stelle",
    "lies", "lese", "zeig", "zeige", "hol", "hole", "ruf", "rufe", "hör", "höre",
})
_SEPARABLE_PARTICLES = frozenset({"an", "aus", "ein", "mit", "vor", "zu", "zurück"})
_COMMAND_FILLERS = frozenset({"mal", "kurz", "bitte", "eben", "schnell"})
_MODAL_OR_WISH_VERBS = frozenset({
    "will", "willst", "wollen", "wollt", "wollte", "wollten", "würde",
    "würdest", "würden", "möchte", "möchtest", "möchten", "muss", "musst",
    "müssen", "müsste", "müsstest", "müssten", "soll", "sollst", "sollte",
    "solltest", "sollten", "kann", "kannst", "können", "könnt", "könnte",
    "könntest", "könnten", "habe", "hast", "hat", "haben", "hatte", "bin",
    "bist", "ist", "sind", "seid", "war", "waren", "wäre", "wären", "hätte",
    "hättest", "hätten", "werde", "wirst", "wird", "werden", "wurde", "wurden",
})
_QUESTION_FUNCTION_WORDS = frozenset({
    "ich", "du", "er", "sie", "es", "wir", "ihr", "mich", "mir", "dich",
    "dir", "ihn", "ihm", "uns", "euch", "mein", "meine", "meinen", "meinem",
    "meiner", "dein", "deine", "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einen", "einem", "einer", "eines", "am", "im", "zum",
    "zur", "mit", "von", "für", "an", "auf", "bei", "in", "über", "unter",
})
_COMMAND_VERBS = _SEPARABLE_COMMANDS | frozenset({
    "spiel", "spiele", "sag", "sage", "erzähl", "erzähle", "öffne", "öffn",
    "schließe", "schließ", "schick", "schicke",
})
_COMMAND_COMPLEMENTS = frozenset({"hoch", "runter", "ab", "lauter", "leiser"})
_FULL_VERBS = frozenset({
    "abhaken", "abholen", "anfangen", "ankommen", "anrufen", "aufmachen",
    "aufräumen", "ausschalten", "einkaufen", "einschalten", "eintragen",
    "erklären", "erinnern", "erzählen", "finden", "gehen", "helfen", "hören",
    "kochen", "lesen", "machen", "mitbringen", "öffnen", "planen", "reden", "schicken",
    "schlafen", "schreiben", "sehen", "senden", "spielen", "stoppen", "suchen",
    "trinken", "vorlesen", "warten", "zeigen", "zuhören", "mergen", "merge",
    "reviewen", "testen", "fixen", "pushen", "deployen", "builden",
})
_FINITE_FULL_VERBS = frozenset({
    "bin", "bist", "ist", "sind", "seid", "war", "waren", "stehe", "steht",
    "stehen", "gehe", "geht", "gehen", "komme", "kommt", "kommen", "mache",
    "macht", "spielen", "spiele", "spielt", "schlafe", "schläft", "schlafen",
    "warte", "wartet", "warten", "kostet", "läuft", "liege", "liegt", "schreibt",
    "lese", "liest", "hört", "sehe", "sieht", "finde", "findet", "trinke",
    "trinkt", "danke",
})
_SHORT_COMPLETE_UTTERANCES = frozenset({
    "stopp", "stop", "danke", "pause", "weiter", "lauter", "leiser",
})
_CONTENT_FUNCTION_WORDS = _QUESTION_FUNCTION_WORDS | frozenset({
    "bitte", "gerne", "gern", "vielleicht", "auch", "noch", "sehr", "mehr",
    "weniger", "viel", "wenig",
})


class UtteranceStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


_MODAL_VERBS = frozenset({
    "will", "willst", "wollen", "wollt", "wollte", "wollten", "würde",
    "würdest", "würden", "möchte", "möchtest", "möchten", "muss", "musst",
    "müssen", "müsste", "müsstest", "müssten", "soll", "sollst", "sollt",
    "sollen", "sollte", "solltest", "sollten", "kann", "kannst", "können",
    "könnt", "könnte", "könntest", "könnten",
})
_DETERMINERS = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
    "einem", "einer", "eines", "mein", "meine", "meinen", "meinem",
    "meiner", "meines", "dein", "deine", "deinen", "deinem", "deiner",
    "deines", "unser", "unsere", "unseren", "unserem", "unserer", "unseres",
    "dieser", "diese", "dieses", "diesen", "diesem", "welcher", "welche",
    "welches", "welchen", "welchem",
})
_OPEN_PREPOSITIONS = frozenset({
    "an", "auf", "aus", "bei", "durch", "für", "gegen", "hinter", "in",
    "neben", "ohne", "über", "um", "unter", "von", "vor", "zwischen",
    "mit", "nach", "zu", "ab",
})
_OPEN_CONJUNCTIONS = _SUBORDINATORS | frozenset({"und", "oder", "aber", "denn"})
_OPEN_FILLERS = _COMMAND_FILLERS | frozenset({
    "ähm", "hm", "also", "vielleicht", "gerne", "gern", "danach", "viel", "wieviel",
})
_ATTRIBUTIVE_ADJECTIVES = frozenset({
    "beste", "besten", "bester", "bestes", "bestem", "gut", "gute", "guten",
    "guter", "gutes", "gutem", "neu", "neue", "neuen", "neuer", "neues",
    "neuem", "schön", "schöne", "schönen", "schöner", "schönes", "schönem",
    "groß", "große", "großen", "großer", "großes", "großem", "klein", "kleine",
    "kleinen", "kleiner", "kleines", "kleinem", "aktuell", "aktuelle", "aktuellen",
    "aktueller", "aktuelles", "aktuellem", "wichtig", "wichtige", "wichtigen",
    "wichtiger", "wichtiges", "wichtigem", "passend", "passende", "passenden",
    "passender", "passendes", "passendem", "richtig", "richtige", "richtigen",
    "richtiger", "richtiges", "richtigem",
})
_SUPERLATIVE_ADJECTIVE_RE = re.compile(r"^.+(?:ste|sten|ster|stes|stem)$")


def _ends_in_determiner_adjective(words: list[str]) -> bool:
    return (
        len(words) >= 2
        and words[-2] in _DETERMINERS
        and (
            words[-1] in _ATTRIBUTIVE_ADJECTIVES
            or _SUPERLATIVE_ADJECTIVE_RE.fullmatch(words[-1]) is not None
        )
    )


def _ends_in_open_question_phrase(words: list[str]) -> bool:
    return (
        len(words) >= 2
        and words[-2:] in (["am", "besten"], ["am", "schnellsten"])
    ) or _ends_in_determiner_adjective(words)


def _has_modal_without_full_verb(words: list[str]) -> bool:
    return any(
        word in _MODAL_VERBS
        and not any(later in _FULL_VERBS for later in words[index + 1:])
        for index, word in enumerate(words)
    )


def _is_complete_command_particle(words: list[str]) -> bool:
    return (
        words[0] in _SEPARABLE_COMMANDS
        and words[-1] in _SEPARABLE_PARTICLES | _COMMAND_COMPLEMENTS
        and any(
            word not in _CONTENT_FUNCTION_WORDS | _COMMAND_FILLERS
            for word in words[1:-1]
        )
    )


def _is_closed_question_pronoun(words: list[str]) -> bool:
    if words[0] not in _QUESTION_WORDS:
        return False
    for index, word in enumerate(words[1:], start=1):
        after_verb = words[index + 1:]
        if (
            word in _QUESTION_VERBS
            and after_verb
            and after_verb[-1] in _QUESTION_PRONOUNS
            and all(item in _QUESTION_FUNCTION_WORDS for item in after_verb)
        ):
            return True
    return False


def _is_closed_question_particle(words: list[str]) -> bool:
    if words[0] not in _QUESTION_WORDS or words[-1] not in _SEPARABLE_PARTICLES:
        return False
    return any(
        word in _QUESTION_VERBS
        and any(item not in _QUESTION_FUNCTION_WORDS for item in words[index + 1:-1])
        for index, word in enumerate(words[1:-1], start=1)
    )


def _has_open_ending(value: str, words: list[str]) -> bool:
    first, last = words[0], words[-1]
    if first in _SUBORDINATORS | {"und", "oder", "aber"} and "," not in value:
        return True
    if last in _OPEN_CONJUNCTIONS:
        return True
    if _has_modal_without_full_verb(words):
        return True
    if _ends_in_open_question_phrase(words):
        return True
    if last in _OPEN_PREPOSITIONS:
        return not (_is_complete_command_particle(words) or _is_closed_question_particle(words))
    if last in _DETERMINERS and not _is_closed_question_pronoun(words):
        return True
    if last in _OPEN_FILLERS and words != ["wie", "bitte"]:
        return True
    if last in _MODAL_OR_WISH_VERBS:
        return True
    return False


def classify_utterance(text: str) -> UtteranceStatus:
    """Ordnet Teiltexte nur bei klaren Mustern COMPLETE oder INCOMPLETE zu."""
    value = str(text or "").strip()
    words = [word.casefold() for word in _WORD_RE.findall(value)]
    if not words:
        return UtteranceStatus.UNKNOWN

    first, last = words[0], words[-1]
    if _has_open_ending(value, words):
        return UtteranceStatus.INCOMPLETE

    if " ".join(words) in _SHORT_COMPLETE_UTTERANCES:
        return UtteranceStatus.COMPLETE

    if first in _COMMAND_VERBS and last in _SEPARABLE_PARTICLES | _COMMAND_COMPLEMENTS:
        if any(
            word not in _CONTENT_FUNCTION_WORDS | _COMMAND_FILLERS
            for word in words[1:-1]
        ):
            return UtteranceStatus.COMPLETE
        return UtteranceStatus.INCOMPLETE
    if _is_closed_question_particle(words):
        return UtteranceStatus.COMPLETE

    if first in _QUESTION_WORDS:
        for index, word in enumerate(words[1:], start=1):
            if word not in _QUESTION_VERBS:
                continue
            after_verb = words[index + 1:]
            if any(item not in _QUESTION_FUNCTION_WORDS for item in after_verb):
                return UtteranceStatus.COMPLETE
            if (
                after_verb
                and after_verb[-1] in _QUESTION_PRONOUNS
                and all(item in _QUESTION_FUNCTION_WORDS for item in after_verb)
            ):
                return UtteranceStatus.COMPLETE
            if words == ["wie", "spät", "ist", "es"]:
                return UtteranceStatus.COMPLETE

    if first in _COMMAND_VERBS:
        command_content = [
            word for word in words[1:]
            if word not in _CONTENT_FUNCTION_WORDS | _COMMAND_FILLERS
        ]
        if not command_content:
            return UtteranceStatus.INCOMPLETE
        if last in _SEPARABLE_PARTICLES | _COMMAND_COMPLEMENTS:
            return UtteranceStatus.COMPLETE
        # Ein Befehl mit Zeit-/Wertangabe braucht neben dem Objekt noch einen
        # konkreten Zusatz: "stell den Timer auf zehn Minuten".
        if first in {"stell", "stelle"}:
            if "auf" in words[1:-1] and len(command_content) >= 3:
                return UtteranceStatus.COMPLETE
            return UtteranceStatus.INCOMPLETE
        if first in _SEPARABLE_COMMANDS:
            first_argument = next(
                (
                    word for word in words[1:]
                    if word not in _COMMAND_FILLERS
                ),
                None,
            )
            if first_argument in _DETERMINERS:
                return UtteranceStatus.INCOMPLETE
            return UtteranceStatus.UNKNOWN
        return UtteranceStatus.COMPLETE

    if last in _FULL_VERBS:
        content_before_verb = [
            word for word in words[:-1] if word not in _CONTENT_FUNCTION_WORDS
        ]
        if content_before_verb:
            return UtteranceStatus.COMPLETE
        if (
            len(words) >= 3
            and words[0] in {"ich", "du", "er", "sie", "wir", "ihr"}
            and any(word in _MODAL_OR_WISH_VERBS for word in words[:-1])
        ):
            return UtteranceStatus.COMPLETE

    if last in _FINITE_FULL_VERBS and last not in _MODAL_OR_WISH_VERBS:
        if len(words) >= 2 and (
            first in {"ich", "du", "er", "sie", "wir", "ihr"}
            or any(word not in _CONTENT_FUNCTION_WORDS for word in words[:-1])
        ):
            return UtteranceStatus.COMPLETE

    if last in _QUESTION_PRONOUNS:
        return UtteranceStatus.UNKNOWN

    if last in _INCOMPLETE_ENDINGS:
        return UtteranceStatus.INCOMPLETE

    if first in _QUESTION_WORDS:
        return UtteranceStatus.UNKNOWN

    return UtteranceStatus.UNKNOWN


def utterance_is_complete(text: str) -> bool:
    """Kompatibilitätshelfer: True nur für positiv erkannte Abschlüsse."""
    return classify_utterance(text) is UtteranceStatus.COMPLETE

# Silero VAD's ONNX export requires exactly this many samples per inference
# call (32ms @ 16kHz) — confirmed empirically against the downloaded model.
VAD_REQUIRED_SAMPLES = 512


def _load_session(model_path: Path):
    import onnxruntime as ort
    return ort.InferenceSession(str(model_path))


class SileroVAD:
    """Wraps the Silero VAD ONNX model. The public (recent) export exposes a
    single recurrent `state` tensor of shape (2, batch, 128) rather than the
    separate LSTM `h`/`c` tensors used by older exports — verified empirically
    via `s.get_inputs()` / `s.get_outputs()` on the downloaded model:
      inputs:  input [None, None] float32, state [2, None, 128] float32, sr [] int64
      outputs: output [None, 1] float32, stateN [None, None, None] float32

    CONTEXT PREPENDING: Silero VAD v5+'s ONNX graph expects the last 64 samples
    of the PREVIOUS chunk prepended to the current 512-sample chunk (making the
    actual "input" tensor 576 samples) — this is separate from the recurrent
    `state` tensor and is NOT documented in the graph's input shape metadata
    (which just shows `[None, None]`). Confirmed against the official
    `silero-vad` PyPI package's `OnnxWrapper.__call__` (`context_size = 64` for
    16kHz, `x = torch.cat([self._context, x], dim=1)`). Without this, the model
    silently returns near-zero speech probability for real audio containing
    genuine speech — found via a live end-to-end test that fed real recordings
    through the whole VAD+wake-word pipeline (docs/superpowers/plans/2026-07-05-
    vad-wakeword-streaming.md, Task 6) after the original Task 1 implementation
    only ever exercised this class against a mocked ONNX session."""

    CONTEXT_SAMPLES = 64

    def __init__(self, model_path: Path):
        self._session = _load_session(model_path)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT_SAMPLES, dtype=np.float32)

    def speech_probability(self, pcm_chunk: bytes) -> float:
        n_samples = len(pcm_chunk) // 2
        if n_samples != VAD_REQUIRED_SAMPLES:
            raise ValueError(
                f"SileroVAD.speech_probability requires exactly "
                f"{VAD_REQUIRED_SAMPLES} samples ({VAD_REQUIRED_SAMPLES * 2} bytes), "
                f"got {n_samples} samples ({len(pcm_chunk)} bytes). Callers must "
                f"buffer raw audio to this frame size before calling (see "
                f"core/voice_stream.py::VoiceStreamSession)."
            )
        samples = struct.unpack(f"<{n_samples}h", pcm_chunk)
        audio = np.array(samples, dtype=np.float32) / 32768.0
        audio_with_context = np.concatenate([self._context, audio])
        inputs = {
            "input": audio_with_context[np.newaxis, :],
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            "state": self._state,
        }
        outputs = self._session.run(None, inputs)
        if len(outputs) > 1:
            self._state = np.array(outputs[1], dtype=np.float32)
        self._context = audio_with_context[-self.CONTEXT_SAMPLES:]
        return float(np.array(outputs[0]).flatten()[0])


@dataclass
class SegmentEvent:
    audio: bytes
    duration_ms: float
    last_speech_sample_index: int | None = None


class VadSegmenter:
    """Zustandsautomat: sammelt PCM-Chunks, erkennt Sprechbeginn/-ende per VAD-
    Wahrscheinlichkeit, liefert fertige Segmente (inkl. Preroll) zurück. Portierte
    Logik von apps/desktop/src/voice-capture.ts's alter tick()-Funktion.

    Bleibt bewusst agnostisch gegenüber der konkreten chunk_ms-Größe: der Aufrufer
    ist dafür verantwortlich, process_chunk() mit Chunks der angegebenen Dauer zu
    füttern. Der WebSocket-Pfad (web/routers/voice.py) puffert rohe, sehr kleine
    AudioWorklet-Frames serverseitig in core/voice_stream.py::VoiceStreamSession
    zu exakt 512-Sample-Blöcken (32ms @ 16kHz, das von Silero VAD geforderte
    Modell-Input-Format) und ruft process_chunk() entsprechend mit chunk_ms=32
    auf — NICHT mit den früher angenommenen 100ms."""

    SPEECH_THRESHOLD = 0.5

    def __init__(self, vad_model: SileroVAD, chunk_ms: float):
        self._vad = vad_model
        self._chunk_ms = chunk_ms
        self._speaking = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._buffer: list[bytes] = []
        self._buffer_samples = 0
        self._preroll: list[bytes] = []
        self._partial_transcript = ""
        self._partial_transcript_sample_index: int | None = None
        self._last_speech_sample_index: int | None = None
        self._current_chunk_is_speech = False

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def current_audio(self) -> bytes:
        return b"".join(self._buffer)

    @property
    def current_chunk_is_speech(self) -> bool:
        return self._current_chunk_is_speech

    @property
    def last_speech_sample_index(self) -> int | None:
        return self._last_speech_sample_index

    def set_partial_transcript(
        self, text: str, audio_end_sample_index: int | None = None,
    ) -> None:
        self._partial_transcript = str(text or "").strip()
        if not self._partial_transcript:
            self._partial_transcript_sample_index = None
        else:
            self._partial_transcript_sample_index = (
                self._last_speech_sample_index
                if audio_end_sample_index is None else max(0, int(audio_end_sample_index))
            )

    def _silence_threshold_ms(self) -> int:
        base = int(getattr(config, "VOICE_SILENCE_MS", 800))
        if not getattr(config, "VOICE_ADAPTIVE_SILENCE", True):
            return base
        if not self._partial_transcript:
            return base
        status = classify_utterance(self._partial_transcript)
        if status is UtteranceStatus.COMPLETE:
            last_speech = self._last_speech_sample_index
            covered = self._partial_transcript_sample_index
            max_stale_samples = int(
                getattr(config, "VOICE_PARTIAL_STALE_MS", 400) * SAMPLE_RATE / 1000
            )
            if (
                last_speech is not None
                and covered is not None
                and max(0, last_speech - covered) <= max_stale_samples
            ):
                return int(getattr(config, "VOICE_SILENCE_FAST_MS", 450))
            return base
        if status is UtteranceStatus.INCOMPLETE:
            return int(getattr(config, "VOICE_SILENCE_SLOW_MS", 1200))
        return base

    def process_chunk(self, pcm_chunk: bytes) -> SegmentEvent | None:
        prob = self._vad.speech_probability(pcm_chunk)
        is_speech = prob >= self.SPEECH_THRESHOLD
        self._current_chunk_is_speech = is_speech

        if is_speech:
            self._silence_ms = 0.0
            if not self._speaking:
                self._speaking = True
                self._speech_ms = 0.0
                self._buffer = list(self._preroll)
                self._buffer_samples = sum(len(chunk) // 2 for chunk in self._buffer)
                self._last_speech_sample_index = None
            self._speech_ms += self._chunk_ms
            self._buffer.append(pcm_chunk)
            self._buffer_samples += len(pcm_chunk) // 2
            self._last_speech_sample_index = self._buffer_samples
        elif self._speaking:
            self._buffer.append(pcm_chunk)
            self._buffer_samples += len(pcm_chunk) // 2
            self._silence_ms += self._chunk_ms
            if self._silence_ms >= self._silence_threshold_ms():
                self._speaking = False
                total_ms = self._speech_ms + self._silence_ms
                segment = b"".join(self._buffer)
                last_speech_sample_index = self._last_speech_sample_index
                self._buffer = []
                self._buffer_samples = 0
                self._partial_transcript = ""
                self._partial_transcript_sample_index = None
                self._last_speech_sample_index = None
                if self._speech_ms >= MIN_SEGMENT_MS:
                    return SegmentEvent(
                        audio=segment,
                        duration_ms=total_ms,
                        last_speech_sample_index=last_speech_sample_index,
                    )
                return None

        preroll_chunks = max(1, int(PREROLL_MS / self._chunk_ms))
        self._preroll.append(pcm_chunk)
        self._preroll = self._preroll[-preroll_chunks:]
        return None
