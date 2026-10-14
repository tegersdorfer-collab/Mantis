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
        return True
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
