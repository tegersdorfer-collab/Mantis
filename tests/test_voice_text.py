"""Regressionstests für Satzgrenzen in Voice-Antworten."""

import pytest

from core.voice_text import last_complete_sentence, normalize_wakeword_transcript


def test_numeric_sentence_end_keeps_the_complete_sentence():
    assert last_complete_sentence(
        "Dein Puls ist 53. Die Nacht davor war"
    ) == "Dein Puls ist 53."


@pytest.mark.parametrize(
    "mishearing",
    [
        "Meintest", "Mehr das", "Mertes", "Mantasy", "Bantas", "Mann das",
        "Mentiones", "Mendes", "Mandis", "Mentis",
    ],
)
def test_acoustic_wakeword_replaces_known_initial_mishearing(mishearing):
    assert normalize_wakeword_transcript(
        f"{mishearing}, wie spät ist es?", wakeword_triggered=True,
    ) == "Mantis, wie spät ist es?"


@pytest.mark.parametrize(
    "mishearing",
    [
        "Meintest", "Mehr das", "Mertes", "Mantasy", "Bantas", "Mann das",
        "Mentiones", "Mendes", "Mandis", "Mentis",
    ],
)
def test_mantis_like_transcript_is_never_rewritten_without_acoustic_wakeword(mishearing):
    text = f"{mishearing}, wie spät ist es?"

    assert normalize_wakeword_transcript(text, wakeword_triggered=False) == text


def test_acoustic_wakeword_is_added_to_transcript_context_once():
    assert normalize_wakeword_transcript(
        "Mantis wie spät ist es?", wakeword_triggered=True,
    ) == "Mantis, wie spät ist es?"
    assert normalize_wakeword_transcript(
        "Wie spät ist es?", wakeword_triggered=True,
    ) == "Mantis, Wie spät ist es?"


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
