"""Tests für forge/budget.py — Buchführung über die Gratis-Kontingente."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import db


class TestMigrationen:
    def _sql(self):
        return "\n".join(db.MIGRATIONS)

    def test_budget_tabelle_ist_migriert(self):
        sql = self._sql()
        assert "forge_budget" in sql
        for spalte in ("nacht", "provider", "laeufe", "tokens_in", "tokens_out",
                       "erschoepft_seit", "grund"):
            assert spalte in sql, f"Spalte {spalte} fehlt in der Migration"

    def test_primaerschluessel_ist_nacht_und_provider(self):
        assert "PRIMARY KEY (nacht, provider)" in self._sql()

    def test_implement_model_spalte_ist_migriert(self):
        assert "implement_model" in self._sql()

    def test_migrationen_sind_idempotent(self):
        """Jede Migration muss ein zweites Mal laufen können."""
        for m in db.MIGRATIONS:
            gross = m.upper()
            if gross.strip().startswith("CREATE TABLE"):
                assert "IF NOT EXISTS" in gross, m[:80]
            if gross.strip().startswith("ALTER TABLE") and "ADD COLUMN" in gross:
                assert "IF NOT EXISTS" in gross, m[:80]


import re
from datetime import date, datetime, timedelta

import pytest

from forge import budget


class _IntegrityErrorStub(Exception):
    """Simuliert psycopg2.IntegrityError bei einer verletzten
    PRIMARY KEY-Constraint, ohne psycopg2 zu importieren."""


def _leere_zeile(nacht, provider):
    return {"nacht": nacht, "provider": provider, "laeufe": 0,
            "tokens_in": 0, "tokens_out": 0, "erschoepft_seit": None, "grund": None}


def _spalten_aus_select(sql):
    """Liefert die im SELECT genannten Spaltennamen, oder None für SELECT *."""
    treffer = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE | re.DOTALL)
    spalten_text = treffer.group(1).strip()
    if spalten_text == "*":
        return None
    return [s.strip() for s in spalten_text.split(",")]


class TestProviderAusModell:
    def test_opencode_modelle_tragen_den_provider_vorn(self):
        assert budget.provider_von_modell("nvidia/moonshotai/kimi-k3") == "nvidia"
        assert budget.provider_von_modell("google/gemini-3.6-flash") == "google"
        assert budget.provider_von_modell("groq/openai/gpt-oss-120b") == "groq"

    def test_agy_modelle_haben_keinen_praefix(self):
        assert budget.provider_von_modell("claude-opus-4-6-thinking") == "antigravity"
        assert budget.provider_von_modell("gemini-3.1-pro-high") == "antigravity"


class TestNachtSchluessel:
    def test_abend_gehoert_zum_selben_tag(self):
        assert budget.nacht_id(datetime(2026, 9, 9, 23, 30)) == date(2026, 9, 9)

    def test_nach_mitternacht_gehoert_zur_vorherigen_nacht(self):
        """Ein Lauf um 02:00 gehört zur Nacht, die am Vorabend begann."""
        assert budget.nacht_id(datetime(2026, 9, 10, 2, 0)) == date(2026, 9, 9)

    def test_nachmittag_gehoert_zum_selben_tag(self):
        assert budget.nacht_id(datetime(2026, 9, 10, 13, 0)) == date(2026, 9, 10)


@pytest.fixture
def db_stub(monkeypatch):
    """Bildet die reale forge_budget-Tabelle nach — inklusive ihrer
    PRIMARY KEY (nacht, provider)-Constraint aus der Migration.

    Der Stub dispatcht nicht mehr blind per Substring auf hart codierte
    Update-Logik: er prüft zuerst, ob die SQL-Anweisung tatsächlich eine
    wirksame ON CONFLICT ... DO UPDATE-Klausel enthält. Fehlt sie bei
    einem INSERT auf einen bereits vorhandenen Schlüssel, wirft er einen
    Fehler — genau wie Postgres es bei einer verletzten Primary-Key-
    Constraint täte. Nur wenn die Klausel da ist, wird die (weiterhin
    handgeschriebene) Update-Logik für buche()/markiere_erschoepft()
    angewandt. Modul-weite Fixture, damit sie sowohl TestBuchfuehrung als
    auch TestStand zur Verfügung steht.
    """
    zeilen = {}

    def _execute(sql, params=()):
        sql_gross = sql.upper()
        ist_insert = sql_gross.strip().startswith("INSERT")
        hat_on_conflict = "ON CONFLICT" in sql_gross
        hat_do_update = "DO UPDATE" in sql_gross
        schluessel = (params[0], params[1])
        existiert = schluessel in zeilen

        if ist_insert and existiert and not hat_on_conflict:
            # Das ist der Fall, den ein reines INSERT ohne
            # ON CONFLICT-Klausel in echtem Postgres nicht überlebt:
            # doppelter Primary Key.
            raise _IntegrityErrorStub(
                "duplicate key value violates unique constraint "
                f"\"forge_budget_pkey\": Key (nacht, provider)="
                f"{schluessel} already exists."
            )

        if ist_insert and existiert and hat_on_conflict and not hat_do_update:
            # z.B. ON CONFLICT ... DO NOTHING: bewusst keine Änderung.
            return 0

        if "ERSCHOEPFT_SEIT" in sql_gross:
            # markiere_erschoepft(): INSERT ... ON CONFLICT DO UPDATE SET
            # erschoepft_seit = NOW(), grund = EXCLUDED.grund. Zähler
            # bleiben unangetastet; ein frischer INSERT legt die Zeile
            # mit Nullzählern an.
            grund = params[2]
            eintrag = zeilen.setdefault(schluessel, _leere_zeile(*schluessel))
            eintrag["erschoepft_seit"] = "jetzt"
            eintrag["grund"] = grund
            return 1

        # buche(): INSERT ... ON CONFLICT DO UPDATE SET
        # laeufe = laeufe + 1, tokens_in = ..., tokens_out = ...
        eintrag = zeilen.setdefault(schluessel, _leere_zeile(*schluessel))
        eintrag["laeufe"] += 1
        eintrag["tokens_in"] += params[2]
        eintrag["tokens_out"] += params[3]
        return 1

    def _query_one(sql, params=()):
        zeile = zeilen.get((params[0], params[1]))
        if zeile is None:
            return None
        spalten = _spalten_aus_select(sql)
        if spalten is None:
            return dict(zeile)
        return {spalte: zeile.get(spalte) for spalte in spalten}

    def _query(sql, params=()):
        # Bildet WHERE nacht = %s und ORDER BY provider nach - stand()
        # darf keine Zeilen anderer Nächte zeigen und muss sortiert sein.
        nacht = params[0] if params else None
        treffer = [dict(z) for z in zeilen.values()
                   if nacht is None or z["nacht"] == nacht]
        if "ORDER BY PROVIDER" in sql.upper():
            treffer.sort(key=lambda z: z["provider"])
        return treffer

    monkeypatch.setattr(budget.db, "execute", _execute)
    monkeypatch.setattr(budget.db, "query_one", _query_one)
    monkeypatch.setattr(budget.db, "query", _query)
    return zeilen


class TestBuchfuehrung:
    def test_buchen_summiert(self, db_stub):
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        budget.buche("nvidia/minimaxai/minimax-m3", 200, 20)
        eintrag = db_stub[(budget.nacht_id(), "nvidia")]
        assert eintrag["laeufe"] == 2
        assert eintrag["tokens_in"] == 300
        assert eintrag["tokens_out"] == 30

    def test_provider_werden_getrennt_gefuehrt(self, db_stub):
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        budget.buche("google/gemini-3.6-flash", 50, 5)
        assert db_stub[(budget.nacht_id(), "nvidia")]["tokens_in"] == 100
        assert db_stub[(budget.nacht_id(), "google")]["tokens_in"] == 50

    def test_frischer_provider_ist_nicht_erschoepft(self, db_stub):
        assert budget.ist_erschoepft("nvidia/moonshotai/kimi-k3") is False

    def test_markierte_erschoepfung_gilt(self, db_stub):
        budget.buche("claude-opus-4-6-thinking", 10, 1)
        budget.markiere_erschoepft("claude-opus-4-6-thinking", "Kontingent leer")
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is True

    def test_erschoepfung_gilt_fuer_den_ganzen_provider(self, db_stub):
        """Nicht nur für das Modell, das die Meldung ausgelöst hat."""
        budget.buche("nvidia/moonshotai/kimi-k3", 10, 1)
        budget.markiere_erschoepft("nvidia/moonshotai/kimi-k3", "leer")
        assert budget.ist_erschoepft("nvidia/minimaxai/minimax-m3") is True

    def test_obergrenze_erschoepft_vorsorglich(self, db_stub, monkeypatch):
        monkeypatch.setitem(budget.OBERGRENZEN, "antigravity", 2)
        budget.buche("claude-opus-4-6-thinking", 1, 1)
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is False
        budget.buche("claude-opus-4-6-thinking", 1, 1)
        assert budget.ist_erschoepft("claude-opus-4-6-thinking") is True

    def test_antigravity_grenze_liegt_unter_der_berichteten(self):
        """18 statt der berichteten 20 — die Zahl ist eine Community-Angabe."""
        assert budget.OBERGRENZEN["antigravity"] == 18

    def test_erschoepfung_gilt_auch_ohne_vorherige_buchung(self, db_stub):
        """markiere_erschoepft() muss auch dann wirken, wenn buche() für
        diesen Anbieter in dieser Nacht noch nie lief — sonst existiert noch
        keine Zeile für (nacht, provider), und ein reines UPDATE verpufft
        folgenlos. Das System würde dann bis zum Morgen weiter gegen einen
        Anbieter laufen, der schon abgewiesen hat."""
        budget.markiere_erschoepft("nvidia/moonshotai/kimi-k3", "leer, ohne vorherige Buchung")
        assert budget.ist_erschoepft("nvidia/moonshotai/kimi-k3") is True


class TestStand:
    def test_stand_zeigt_alle_provider_der_nacht_sortiert(self, db_stub):
        budget.buche("google/gemini-3.6-flash", 50, 5)
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        ergebnis = budget.stand()
        assert [zeile["provider"] for zeile in ergebnis] == ["google", "nvidia"]
        assert ergebnis[0]["tokens_in"] == 50
        assert ergebnis[1]["tokens_in"] == 100

    def test_stand_zeigt_nur_die_laufende_nacht(self, db_stub):
        """_query muss nach nacht filtern — eine Zeile aus einer anderen
        Nacht darf in stand() nicht auftauchen."""
        alte_nacht = budget.nacht_id() - timedelta(days=1)
        db_stub[(alte_nacht, "groq")] = {
            "nacht": alte_nacht, "provider": "groq", "laeufe": 5,
            "tokens_in": 1, "tokens_out": 1, "erschoepft_seit": None, "grund": None}
        budget.buche("nvidia/moonshotai/kimi-k3", 100, 10)
        ergebnis = budget.stand()
        assert [zeile["provider"] for zeile in ergebnis] == ["nvidia"]

    def test_stand_ohne_buchungen_ist_leer(self, db_stub):
        assert budget.stand() == []
