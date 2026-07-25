"""Unit-Tests für die deterministische Kernlogik von domains/accountability.py.

Nur reine Funktionen (Textklassifikation, Zeitfenster, statische Nachrichten) —
kein DB, kein LLM. Die DB-gestützte State-Machine (intercept/tick) wird gegen
eine echte Postgres verifiziert, nicht hier."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime

from domains import accountability as a


class TestLooksMultiple:
    def test_kommaliste(self):
        assert a._looks_multiple("Steuer, Wäsche, einkaufen") is True

    def test_mehrere_zeilen(self):
        assert a._looks_multiple("Steuer machen\nSport") is True

    def test_semikolon(self):
        assert a._looks_multiple("x; y") is True

    def test_nummerierte_liste(self):
        assert a._looks_multiple("1. x\n2. y") is True

    def test_einzelne_aufgabe(self):
        assert a._looks_multiple("Präsentation für Kunde X fertig machen") is False

    def test_und_ist_keine_liste(self):
        # bewusst konservativ: ein einzelnes "und" ist oft eine zusammenhängende Aufgabe
        assert a._looks_multiple("Steuererklärung ausfüllen und abschicken") is False

    def test_ein_komma_ok(self):
        assert a._looks_multiple("Kapitel 3 schreiben, ordentlich diesmal") is False


class TestAffirmativeNegative:
    def test_affirmativ(self):
        for t in ["go", "Los!", "los gehts", "ja", "ok", "start", "bereit", "👍"]:
            assert a._is_affirmative(t) is True, t

    def test_nicht_affirmativ(self):
        for t in ["was kostet ein Tesla", "warte kurz", "nö"]:
            assert a._is_affirmative(t) is False, t

    def test_negativ(self):
        for t in ["nein", "-", "nichts", "nicht gelaufen", "gar nichts"]:
            assert a._is_negative(t) is True, t

    def test_output_ist_nicht_negativ(self):
        assert a._is_negative("Kapitel 3 fertig geschrieben") is False


class TestDueWindow:
    def test_direkt_nach_zielzeit(self):
        tgt = datetime(2026, 7, 20, 10, 0)
        assert a._due_at(datetime(2026, 7, 20, 10, 30), tgt) is True

    def test_vor_zielzeit_nicht_faellig(self):
        tgt = datetime(2026, 7, 20, 10, 0)
        assert a._due_at(datetime(2026, 7, 20, 9, 59), tgt) is False

    def test_nach_catchup_fenster_nicht_faellig(self):
        tgt = datetime(2026, 7, 20, 10, 0)
        # CATCHUP_MIN = 180 → nach 3h+1min raus
        assert a._due_at(datetime(2026, 7, 20, 13, 1), tgt) is False


class TestMessages:
    def test_blockstart_mit_prio(self):
        msg = a._block_start_message("Steuererklärung")
        assert "Handy weg" in msg and "Steuererklärung" in msg

    def test_blockstart_ohne_prio(self):
        msg = a._block_start_message(None)
        assert "kein Fokus" in msg

    def test_block_ende_statisch(self):
        assert a._BLOCK_END_MESSAGE == "Fertig? Ein Satz: was ist rausgekommen?"

    def test_german_date(self):
        assert a._german_date(date(2026, 7, 20)).endswith("20.07.2026")

    def test_parse_hhmm(self):
        t = a._parse_hhmm("08:05")
        assert (t.hour, t.minute) == (8, 5)
