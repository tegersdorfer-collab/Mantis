"""Regressionstests für Satzgrenzen in Voice-Antworten."""

import pytest

from core.voice_text import last_complete_sentence


def test_numeric_sentence_end_keeps_the_complete_sentence():
    assert last_complete_sentence(
        "Dein Puls ist 53. Die Nacht davor war"
    ) == "Dein Puls ist 53."


@pytest.mark.parametrize(
    "text",
    [
        "am 12. Oktober",
        "Termin am 12.10. ist morgen",
        "Dr. Müller wartet noch",
        "z. B. wird das morgen vorgelesen",
        "Die Antwort hat kein Satzzeichen am Ende",
    ],
)
def test_date_and_abbreviation_points_do_not_create_false_sentence_ends(text):
    assert last_complete_sentence(text) == text
