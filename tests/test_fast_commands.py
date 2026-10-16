"""Tests für die deterministische Fast-Path-Schicht (core/fast_commands.py).

Wichtiger als jeden Befehl zu fangen ist, Konversation NICHT zu kapern — daher
liegt der Fokus auf den Negativfällen.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fast_commands import has_voice_wakeword, match, match_voice


def _m(text):
    fc = match(text)
    return (fc.tool, fc.args) if fc else None


# ── Positive: Lampe ───────────────────────────────────────────────────────────

def test_lamp_on_short():
    assert _m("lampe an") == ("lampe", {"action": "an"})
    assert _m("licht an") == ("lampe", {"action": "an"})


def test_lamp_off_short():
    assert _m("licht aus") == ("lampe", {"action": "aus"})


def test_lamp_on_with_verb():
    assert _m("mach die lampe an") == ("lampe", {"action": "an"})
    assert _m("schalte das licht an") == ("lampe", {"action": "an"})


def test_lamp_off_with_verb():
    assert _m("schalt die schreibtischlampe aus") == ("lampe", {"action": "aus"})


def test_lamp_colors():
    assert _m("mach die lampe blau") == ("lampe", {"action": "blau"})
    assert _m("lampe grün") == ("lampe", {"action": "gruen"})
    assert _m("stell das licht auf warmweiß") == ("lampe", {"action": "warmweiss"})


def test_lamp_brightness():
    assert _m("mach die lampe heller") == ("lampe", {"action": "heller"})
    assert _m("licht dunkler") == ("lampe", {"action": "dunkler"})


def test_lamp_brightness_extremes_use_all_three_steps():
    assert _m("lampe so hell wie möglich machen") == ("lampe", {"action": "heller", "schritte": 3})
    assert _m("mach das licht ganz dunkel") == ("lampe", {"action": "dunkler", "schritte": 3})
    assert _m("lampe maximal hell") == ("lampe", {"action": "heller", "schritte": 3})
    assert _m("mach die lampe heller") == ("lampe", {"action": "heller"})


# ── Positive: Roboter-Not-Stopp ───────────────────────────────────────────────

def test_robot_stop():
    assert _m("roboter stopp") == ("robot_control", {"action": "stopp"})
    assert _m("stopp den roboter") == ("robot_control", {"action": "stopp"})


def test_hard_stop_keyword():
    assert _m("notaus") == ("robot_control", {"action": "stopp"})
    assert _m("nothalt") == ("robot_control", {"action": "stopp"})


# ── Negativ: KEINE Fast-Path (Konversation nicht kapern) ──────────────────────

def test_question_never_matches():
    assert match("ist die lampe an?") is None
    assert match("kannst du das licht anmachen?") is None  # Frage → Agent


def test_statement_not_command():
    assert match("die lampe ist aus") is None          # kein Kommando-Verb, kurz aber Aussage
    assert match("ich finde die lampe zu hell hier drüben") is None


def test_bare_stop_not_robot():
    assert match("stopp mal, das stimmt so nicht") is None  # 'stopp' ohne Roboter


def test_unrelated_conversation():
    assert match("erzähl mir was über lampen") is None   # 'lampen' != 'lampe'
    assert match("wie fühlst du dich heute?") is None
    assert match("mach mir bitte einen guten vorschlag fürs abendessen") is None


def test_empty():
    assert match("") is None


# ── Positive: Musik (Spotify) ─────────────────────────────────────────────────

def test_music_play():
    assert _m("musik an") == ("spotify", {"action": "play"})
    assert _m("musik weiter") == ("spotify", {"action": "play"})
    assert _m("spiel musik") == ("spotify", {"action": "play"})
    assert _m("mach die musik wieder an") == ("spotify", {"action": "play"})


def test_music_pause():
    assert _m("musik pause") == ("spotify", {"action": "pause"})
    assert _m("stopp die musik") == ("spotify", {"action": "pause"})
    assert _m("mach die musik aus") == ("spotify", {"action": "pause"})


def test_music_next_prev():
    assert _m("nächstes lied") == ("spotify", {"action": "next"})
    assert _m("nächster song") == ("spotify", {"action": "next"})
    assert _m("musik zurück") == ("spotify", {"action": "previous"})


def test_music_single_word_commands():
    assert _m("pause") == ("spotify", {"action": "pause"})
    assert _m("skip") == ("spotify", {"action": "next"})
    assert _m("next") == ("spotify", {"action": "next"})


# ── Negativ: Musik kapert keine Konversation ─────────────────────────────────

def test_pause_in_sentence_not_music():
    assert match("ich mach mal pause") is None
    assert match("lass uns eine pause machen") is None


def test_weiter_in_conversation_not_music():
    assert match("weiter gehts mit dem projekt") is None
    assert match("erzähl weiter") is None


def test_music_question_never_matches():
    # "welches lied ist das?" fragt nach Identifikation, nicht nach dem
    # Wiedergabe-Status → kein Fast-Path. ("läuft gerade musik?" ist dagegen ein
    # echter Status-Query, siehe test_music_status.)
    assert match("welches lied ist das?") is None


def test_music_statement_not_command():
    assert match("die musik ist aus") is None
    assert match("ich höre gerade musik und mach dann weiter") is None


def test_music_play_with_source_goes_to_agent():
    # „mach die musik von queen an" — Steuer-Phrasierung mit Quelle: bleibt beim
    # Agenten (nur „spiel [X]" wird als Suche deterministisch gefangen).
    assert match("mach die musik von queen an") is None


# ── Positive: Status & Spiel (deterministisch statt unzuverlässiges gemma) ────

def test_music_status():
    assert _m("was läuft gerade") == ("spotify", {"action": "status"})
    assert _m("was läuft gerade?") == ("spotify", {"action": "status"})  # Frage → trotzdem Status
    assert _m("was spielt gerade") == ("spotify", {"action": "status"})
    assert _m("läuft gerade musik?") == ("spotify", {"action": "status"})
    assert _m("welches lied läuft") == ("spotify", {"action": "status"})


def test_spiel_query():
    assert _m("spiel kids von mgmt") == ("spotify", {"action": "spiel", "query": "kids von mgmt"})
    assert _m("spiele bohemian rhapsody") == ("spotify", {"action": "spiel", "query": "bohemian rhapsody"})


def test_spiel_playlist_type_hint():
    assert _m("spiel die playlist focus mix") == (
        "spotify", {"action": "spiel", "query": "die playlist focus mix", "typ": "playlist"})


# ── Negativ: Status/Spiel kapern keine Konversation ──────────────────────────

def test_status_not_general_laeuft():
    assert match("wie läuft dein tag?") is None      # 'läuft' ohne was/musik/welches
    assert match("wie läuft es bei dir") is None


def test_spiel_control_words_not_query():
    # reine Steuerkommandos landen im play/pause-Pfad oder beim Agenten, nicht als Suche
    assert _m("spiel musik") == ("spotify", {"action": "play"})   # Musik-Kontext → play
    assert match("spiel weiter") is None                          # reines Steuerwort, kein Query


# ── Positive: UI-Automatik-Entry (gemma wählt computer_task sonst unzuverlässig) ─

def test_computer_task_routing():
    fc = match("bediene die app notizen und erstelle eine neue notiz")
    assert fc is not None
    assert fc.tool == "computer_task"
    assert "notizen" in fc.args["goal"].lower()


def test_computer_task_steuere():
    fc = match("steuere die app safari")
    assert fc and fc.tool == "computer_task"


# ── Negativ: kein Fehlalarm ───────────────────────────────────────────────────

def test_uiauto_needs_app_word():
    assert match("bediene dich am buffet") is None      # kein 'app'
    assert match("steuere die heizung") is None          # kein 'app'


def test_uiauto_question_not_command():
    assert match("welche app soll ich bedienen?") is None


# ── Positive: Ventilator ──────────────────────────────────────────────────────

def test_fan_on_off():
    assert _m("ventilator an") == ("ventilator", {"action": "an"})
    assert _m("ventilator aus") == ("ventilator", {"action": "aus"})
    assert _m("mach den ventilator an") == ("ventilator", {"action": "an"})


def test_fan_speed():
    assert _m("ventilator stärker") == ("ventilator", {"action": "staerker"})
    assert _m("ventilator schwächer") == ("ventilator", {"action": "schwaecher"})
    assert _m("mach den ventilator schneller") == ("ventilator", {"action": "staerker"})


def test_fan_negatives():
    assert match("läuft der ventilator?") is None            # Frage
    assert match("ich hätte gern einen ventilator") is None  # kein Kommando-Verb, zu lang


# ── Positive: Email-Lese-Abfrage (inhärent Frage; gemma erfindet sonst) ────────

def test_email_unread_query():
    assert _m("hab ich neue mails?") == ("email_unread", {})
    assert _m("ungelesene mails") == ("email_unread", {})
    assert _m("zeig mir den posteingang") == ("email_unread", {})
    assert _m("gibt es neue emails?") == ("email_unread", {})


def test_email_negatives():
    assert match("schreib eine mail an bob") is None       # Senden → Agent (braucht Params)
    assert match("wie war dein tag?") is None
    assert match("ist die mail von der bank wichtig?") is None  # kein neu/posteingang


# ── Positive/Negative: Health-Status ──────────────────────────────────────────

def test_health_status_query():
    assert _m("wie ist meine recovery heute") == ("get_health_scores", {})
    assert _m("wie ist mein gesundheitszustand?") == ("get_health_scores", {})
    assert _m("wie geht es mir gesundheitlich?") == ("get_health_scores", {})


def test_health_status_negatives():
    # 'spiel recovery ...' ist Musik-Suche, keine Health-Frage.
    assert _m("spiel recovery von frank ocean") != ("get_health_scores", {})
    # Ohne Frage-Rahmen kein Health-Fast-Path.
    assert _m("öffne die health app") is None


# ── Positive/Negative: Skilltree-Status ───────────────────────────────────────

def test_skilltree_query_matches():
    fc = match("wie ist mein level?")
    assert fc is not None and fc.tool == "get_skilltree"


def test_fortschritt_does_not_hijack_goals():
    # "fortschritt" is a goals keyword; a fortschritt question must NOT be force-routed to skilltree
    fc = match("wie ist mein fortschritt beim ziel?")
    assert fc is None or fc.tool != "get_skilltree"


def test_voice_time_date_and_weekday_are_local_reflexes():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 14, 9, 7, tzinfo=ZoneInfo("Europe/Berlin"))

    time = match_voice("Wie spät ist es?", now=now)
    date = match_voice("Welches Datum ist heute?", now=now)
    weekday = match_voice("Welcher Wochentag ist heute?", now=now)

    assert time is not None and time.tool == "voice_reflex"
    assert time.args["response"] == "Es ist 09:07 Uhr."
    assert date is not None and "14. Oktober 2026" in date.args["response"]
    assert weekday is not None and weekday.args["response"] == "Heute ist Mittwoch."


def test_voice_reflex_does_not_capture_location_or_normal_questions():
    assert match_voice("Wie spät ist es in Tokio?") is None
    assert match_voice("Wie wird mein Tag?") is None
    assert match_voice("Fünf Minuten sollten reichen") is None
    assert match_voice("Die Musik wird lauter") is None


@pytest.mark.parametrize("text", [
    "Mantis. Wie spät ist es?",
    "Mantis: wie spät ist es",
    "Hey Mantis! Wie spät ist es?",
])
def test_voice_reflex_accepts_sentence_punctuation_after_wakeword(text):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 14, 12, 34, tzinfo=ZoneInfo("Europe/Berlin"))
    command = match_voice(text, now=now)

    assert command is not None and command.tool == "voice_reflex"
    assert command.args["response"] == "Es ist 12:34 Uhr."


@pytest.mark.parametrize("prefix", [
    "Mantis", "mantis", "Mäntis", "Mann das", "Man tis", "Mentis", "Manthis",
    "Hey Mantis", "hey mantis", "Okay Mantis", "okay mantis",
])
@pytest.mark.parametrize("separator", [" ", ", "])
def test_voice_reflex_strips_leading_mantis_wakeword_variants(prefix, separator):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 14, 12, 34, tzinfo=ZoneInfo("Europe/Berlin"))
    command = match_voice(f"{prefix}{separator}wie spät ist es?", now=now)

    assert command is not None and command.tool == "voice_reflex"
    assert command.args["response"] == "Es ist 12:34 Uhr."


def test_voice_reflex_wakeword_variants_are_only_removed_at_sentence_start():
    assert match_voice("Ich sagte Mantis, wie spät ist es?") is None


def test_ambiguous_mann_das_is_removed_only_when_remainder_is_a_reflex():
    command = match_voice("Mann, das wie spät ist es?")

    assert command is not None and command.tool == "voice_reflex"
    assert match_voice("Mann, das ist blöd") is None


@pytest.mark.parametrize("text", [
    "Mann, das Timer auf 5 Minuten",
    "Mann, das lauter",
    "Mann das stopp den Timer",
    "Mann, das Licht aus",
])
def test_ambiguous_mann_das_does_not_trigger_non_reflex_commands(text):
    assert match_voice(text) is None
    assert has_voice_wakeword(text) is False


def test_mantas_is_not_a_voice_wakeword_variant():
    text = "mantas, wie spät ist es?"

    assert match_voice(text) is None
    assert has_voice_wakeword(text) is False


def test_voice_stop_reflex_is_exact():
    stop = match_voice("Stopp.")

    assert stop is not None
    assert stop.tool == "voice_reflex"
    assert stop.args == {"response": "Okay."}


@pytest.mark.parametrize("text", ["warte", "sei still"])
def test_voice_stop_reflex_accepts_additional_stop_phrases(text):
    command = match_voice(text)

    assert command is not None
    assert command.tool == "voice_reflex"
    assert command.args == {"response": "Okay."}


def test_voice_okay_is_a_no_side_effect_reflex():
    okay = match_voice("Okay.")

    assert okay is not None
    assert okay.tool == "voice_reflex"
    assert okay.args == {"response": "Okay."}


def test_voice_timer_and_volume_commands_are_narrow_and_deterministic():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 14, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    timer = match_voice("Stelle einen Timer auf fünf Minuten", now=now)
    natural_timer = match_voice("Stelle den Timer für zwei Minuten", now=now)
    from core.timeparse import parse_datetime

    stop = match_voice("Timer abbrechen", now=now)
    louder = match_voice("Musik lauter", now=now)
    quieter = match_voice("leiser", now=now)

    assert timer is not None and timer.tool == "set_reminder"
    assert timer.args == {
        "text": "Timer: 5 Minuten",
        "at": "2026-10-14T10:05:00",
        "kind": "timer",
    }
    assert parse_datetime(timer.args["at"]) is not None
    assert natural_timer is not None and natural_timer.args == {
        "text": "Timer: 2 Minuten",
        "at": "2026-10-14T10:02:00",
        "kind": "timer",
    }
    assert stop is not None and stop.tool == "stop_timer"
    assert louder is not None and louder.args == {"action": "volume_up"}
    assert quieter is not None and quieter.args == {"action": "volume_down"}


def test_voice_music_pause_and_continue_reuse_existing_fast_commands():
    assert match_voice("Musik Pause").args == {"action": "pause"}
    assert match_voice("Musik weiter").args == {"action": "play"}
