"""Hilfsfunktionen für verständliche, vollständige Voice-Antworten."""
import re


_SENTENCE_END = re.compile(r"[.!?…](?:[\"'»”’)}\]]*)(?=\s|$)")
_MONTH_NAME = re.compile(
    r"\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember)\b",
    re.IGNORECASE,
)
_ABBREVIATION_BEFORE_PERIOD = re.compile(
    r"\b(?:z\.\s*b|u\.\s*a|d\.\s*h|i\.\s*d\.\s*r|u\.\s*ä|"
    r"e\.\s*g|i\.\s*e|etc|usw|bzw|vgl|ca|nr|abs|art|vs|dr|prof|fr|hr|"
    r"st|geb|bspw)\s*$",
    re.IGNORECASE,
)
_ACOUSTIC_WAKEWORD_PREFIX = re.compile(
    r"^\s*(?:meintest|mehr[\s-]+das|mertes|mantasy|bantas|mann[\s-]+das|"
    r"mentiones|mendes|mandis|mentis|mantas|mantis)\b[\s,;:.!?-]*(?P<rest>.*)$",
    re.IGNORECASE,
)
_INITIAL_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_WAKEWORD_INTRODUCTIONS = frozenset({
    "hey", "hallo", "ok", "okay", "äh", "ähm", "also", "yo",
})


def _wakeword_prefix_parts(text: str, *, max_words: int = 2):
    value = str(text or "").strip()
    words = list(_INITIAL_WORD.finditer(value))
    for index, word in enumerate(words[:min(max_words, 2)]):
        if (
            index > 0
            and words[index - 1].group().casefold() not in _WAKEWORD_INTRODUCTIONS
        ):
            continue
        match = _ACOUSTIC_WAKEWORD_PREFIX.match(value[word.start():])
        if match is not None:
            return (
                word.start(),
                word.start() + match.start("rest"),
                match.group("rest"),
            )
    return None


def has_mantis_like_wakeword_prefix(text: str, *, max_words: int = 2) -> bool:
    """Erkennt ein normalisierbares Mantis-Wort in den ersten zwei STT-Wörtern."""
    return _wakeword_prefix_parts(text, max_words=max_words) is not None


def has_exact_initial_mantis(text: str) -> bool:
    """Prüft nur das erste STT-Wort auf das exakt erkannte Wort „Mantis“."""
    words = _INITIAL_WORD.finditer(str(text or "").strip())
    first_word = next(words, None)
    return first_word is not None and first_word.group().casefold() == "mantis"


def normalize_wakeword_transcript(text: str, *, wakeword_triggered: bool) -> str:
    """Setzt eine akustisch bestätigte Anrede für den Gesprächskontext voran."""
    raw = str(text or "")
    if not wakeword_triggered:
        return raw
    value = raw.strip()
    if not value:
        return value
    parts = _wakeword_prefix_parts(value)
    if parts is not None:
        start, _rest_start, raw_rest = parts
        before = value[:start].strip()
        rest = raw_rest.strip()
        content = " ".join(part for part in (before, rest) if part)
        return f"Mantis, {content}" if content else "Mantis"
    return f"Mantis, {value}"


def _is_date_or_abbreviation_period(value: str, match: re.Match) -> bool:
    if value[match.start()] != ".":
        return False

    before = value[:match.start()]
    after = value[match.end():]
    last_word = before.rstrip().split()[-1:]
    if (
        last_word
        and len(last_word[0]) == 1
        and last_word[0].isalpha()
        and re.match(r"\s*[A-Za-zÄÖÜäöüß]\.(?=\s|$)", after)
    ):
        return True
    if re.search(r"(?<!\d)\d{1,2}\.\d{1,2}$", before):
        # 3.11. ohne Jahr ist Teil des Datums – außer es folgt ein neuer Satz (Großbuchstabe).
        return not re.match(r"\s+[A-ZÄÖÜ]", after)
    if re.search(r"(?<!\d)\d{1,2}\.\d{1,2}\.\d{2,4}$", before):
        return False
    if re.search(r"(?<!\d)\d{1,2}$", before) and _MONTH_NAME.match(after):
        return True
    return bool(_ABBREVIATION_BEFORE_PERIOD.search(before))


def last_complete_sentence(text: str) -> str:
    """Gibt vollständige Sätze zurück, sonst den verfügbaren Text bis zum Wortende."""
    value = str(text or "").strip()
    endings = [
        match for match in _SENTENCE_END.finditer(value)
        if not _is_date_or_abbreviation_period(value, match)
    ]
    if not endings:
        return value
    return value[:endings[-1].end()].strip()


def _transcript_words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", str(text or "").casefold(), flags=re.UNICODE)


def partial_text_matches_final(partial: str, final: str) -> bool:
    """Prüft, ob ein finales Transkript einem Teiltext entspricht oder ihn
    höchstens um drei vollständige Wörter erweitert."""
    partial_words = _transcript_words(partial)
    final_words = _transcript_words(final)
    if not partial_words or len(final_words) < len(partial_words):
        return False
    return (
        final_words[:len(partial_words)] == partial_words
        and len(final_words) - len(partial_words) <= 3
    )
