"""Unit-Tests für core/timeparse.py (LLM-Zeitangaben-Parser, kein DB nötig)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import warnings
from datetime import date, datetime, timedelta

from core.timeparse import parse_datetime, parse_date, _next_weekday


class TestRelative:
    def test_morgen_mit_uhrzeit(self):
        dt = parse_datetime("morgen 14:00")
        expected = date.today() + timedelta(days=1)
        assert dt.date() == expected
        assert (dt.hour, dt.minute) == (14, 0)

    def test_morgen_ohne_uhrzeit_default_9(self):
        dt = parse_datetime("morgen")
        assert dt.date() == date.today() + timedelta(days=1)
        assert (dt.hour, dt.minute) == (9, 0)

    def test_uebermorgen(self):
        dt = parse_datetime("übermorgen 8:30")
        assert dt.date() == date.today() + timedelta(days=2)
        assert (dt.hour, dt.minute) == (8, 30)

    def test_heute_mit_um(self):
        dt = parse_datetime("heute um 18:30")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (18, 30)

    def test_in_minuten(self):
        dt = parse_datetime("in 30 minuten")
        expected = datetime.now() + timedelta(minutes=30)
        assert abs((dt.replace(tzinfo=None) - expected).total_seconds()) < 5

    def test_in_stunden(self):
        dt = parse_datetime("in 2 stunden")
        expected = datetime.now() + timedelta(hours=2)
        assert abs((dt.replace(tzinfo=None) - expected).total_seconds()) < 5

    def test_in_tagen(self):
        dt = parse_datetime("in 3 tagen")
        expected = datetime.now() + timedelta(days=3)
        assert abs((dt.replace(tzinfo=None) - expected).total_seconds()) < 5

    def test_jetzt(self):
        dt = parse_datetime("jetzt")
        assert abs((dt.replace(tzinfo=None) - datetime.now()).total_seconds()) < 5


class TestWeekday:
    def test_naechster_wochentag_liegt_in_zukunft(self):
        dt = parse_datetime("montag 10:00")
        assert dt.weekday() == 0
        assert dt.date() > date.today()
        assert (dt.hour, dt.minute) == (10, 0)

    def test_naechsten_prefix(self):
        dt = parse_datetime("nächsten freitag 14:00")
        assert dt.weekday() == 4
        assert dt.date() > date.today()

    def test_next_weekday_never_today(self):
        now = datetime.now()
        # Der eigene Wochentag muss auf nächste Woche fallen, nicht heute
        result = _next_weekday(now, now.weekday())
        assert result.date() == (now + timedelta(days=7)).date()


class TestAbsolute:
    def test_volles_datum_mit_zeit(self):
        dt = parse_datetime("24.12.2026 18:00")
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 12, 24, 18)

    def test_datum_ohne_jahr_ergaenzt_jahr(self):
        dt = parse_datetime("24.12.")
        assert (dt.month, dt.day) == (12, 24)
        # Nie in der Vergangenheit
        assert dt.date() >= date.today()

    def test_nur_uhrzeit_ist_heute(self):
        dt = parse_datetime("15:45")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (15, 45)

    def test_iso_format(self):
        dt = parse_datetime("2026-08-01 09:00")
        assert (dt.year, dt.month, dt.day) == (2026, 8, 1)

    def test_html_datetime_local(self):
        dt = parse_datetime("2026-07-10T09:30")
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 10, 9, 30)


class TestPython315Kompatibel:
    """Ab Python 3.15 ändert sich das Default-Jahr bei strptime ohne Jahresangabe
    (gh-70647) — der Parser darf sich nicht auf den 1900-Default verlassen."""

    def test_kein_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            parse_datetime("24.12.")
            parse_datetime("24.12. 18:00")
            parse_datetime("15:45")
            parse_datetime("morgen 18 uhr")
            parse_datetime("blafasel xyz")
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations == [], [str(w.message) for w in deprecations]

    def test_jahrloses_datum_in_vergangenheit_faellt_auf_naechstes_jahr(self):
        gestern = date.today() - timedelta(days=1)
        if (gestern.month, gestern.day) == (2, 29):  # 29.02. existiert nicht jedes Jahr
            gestern -= timedelta(days=1)
        dt = parse_datetime(f"{gestern.day:02d}.{gestern.month:02d}.")
        assert (dt.month, dt.day) == (gestern.month, gestern.day)
        assert dt.date() > date.today()

    def test_jahrloses_datum_mit_uhrzeit(self):
        dt = parse_datetime("24.12. 18:00")
        assert (dt.month, dt.day, dt.hour, dt.minute) == (12, 24, 18, 0)
        assert dt.date() >= date.today()


class TestRobustness:
    def test_leerer_string(self):
        assert parse_datetime("") is None

    def test_unsinn(self):
        assert parse_datetime("blafasel xyz") is None

    def test_ergebnis_ist_tz_aware(self):
        dt = parse_datetime("morgen 12:00")
        assert dt.tzinfo is not None

    def test_parse_date(self):
        d = parse_date("morgen")
        assert d == date.today() + timedelta(days=1)

    def test_parse_date_none(self):
        assert parse_date("quatsch") is None


class TestWortgrenzen:
    """Wochentagskürzel (mo/di/mi/do/fr/sa/so) dürfen nicht den Anfang
    beliebiger Wörter matchen. Verifiziert am 07.09.2026."""

    def test_sofort_ist_kein_sonntag(self):
        dt = parse_datetime("sofort")
        assert abs((dt.replace(tzinfo=None) - datetime.now()).total_seconds()) < 5

    def test_diesen_montag_ist_kein_dienstag(self):
        dt = parse_datetime("diesen montag 10:00")
        assert dt.weekday() == 0
        assert (dt.hour, dt.minute) == (10, 0)

    def test_fruehstueck_ist_kein_freitag(self):
        dt = parse_datetime("frühstück 8:00")
        assert dt is None or dt.weekday() != 4 or (dt.hour, dt.minute) == (8, 0)

    def test_sommerfest_ist_kein_sonntag(self):
        dt = parse_datetime("sommerfest 14:00")
        assert dt is None or dt.weekday() != 6 or (dt.hour, dt.minute) == (14, 0)

    def test_morgens_ist_nicht_morgen(self):
        dt = parse_datetime("morgens 8:00")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (8, 0)

    def test_mittags_ist_kein_mittwoch(self):
        dt = parse_datetime("mittags 12:00")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (12, 0)


class TestUhrzeitFormate:
    def test_uhr_suffix_bei_wochentag(self):
        dt = parse_datetime("freitag 14:30 uhr")
        assert dt.weekday() == 4
        assert (dt.hour, dt.minute) == (14, 30)

    def test_uhr_suffix_bei_reiner_uhrzeit(self):
        dt = parse_datetime("14:30 uhr")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (14, 30)

    def test_uhr_suffix_bei_heute(self):
        dt = parse_datetime("heute 14:00 uhr")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (14, 0)

    def test_stunde_uhr_minute(self):
        dt = parse_datetime("heute 14 uhr 30")
        assert (dt.hour, dt.minute) == (14, 30)

    def test_stunde_uhr_minute_bei_morgen(self):
        dt = parse_datetime("morgen 9 uhr 30")
        assert dt.date() == date.today() + timedelta(days=1)
        assert (dt.hour, dt.minute) == (9, 30)

    def test_nur_stunde_mit_uhr(self):
        dt = parse_datetime("9 uhr")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (9, 0)

    def test_tageszeit_fueller_wird_ignoriert(self):
        dt = parse_datetime("morgen früh 8:00")
        assert dt.date() == date.today() + timedelta(days=1)
        assert (dt.hour, dt.minute) == (8, 0)

    def test_heute_abend(self):
        dt = parse_datetime("heute abend 20:00")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (20, 0)

    def test_heute_morgen_ist_heute_vormittag(self):
        dt = parse_datetime("heute morgen 10:00")
        assert dt.date() == date.today()
        assert (dt.hour, dt.minute) == (10, 0)


class TestKeinStillerDefault:
    """Eine unlesbare Uhrzeit darf nicht stillschweigend zu 9:00 werden."""

    def test_ungueltige_stunde_ist_none(self):
        assert parse_datetime("morgen 25:00") is None

    def test_ungueltige_minute_ist_none(self):
        assert parse_datetime("morgen 10:75") is None

    def test_unlesbarer_rest_ist_none(self):
        assert parse_datetime("morgen quatschzeit") is None


class TestWochen:
    def test_in_wochen(self):
        dt = parse_datetime("in 2 wochen")
        expected = datetime.now() + timedelta(weeks=2)
        assert abs((dt.replace(tzinfo=None) - expected).total_seconds()) < 5
