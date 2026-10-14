"""Hilfsfunktionen für verständliche, vollständige Voice-Antworten."""
import re


_SENTENCE_END = re.compile(r"(?:(?<!\d)\.|[!?…])(?:[\"'»”’)}\]]*)(?=\s|$)")


def last_complete_sentence(text: str) -> str:
    """Gibt alle vollständigen Sätze bis zum letzten Satzzeichen zurück."""
    value = str(text or "").strip()
    endings = list(_SENTENCE_END.finditer(value))
    if not endings:
        return ""
    return value[:endings[-1].end()].strip()
