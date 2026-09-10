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


from datetime import date, datetime

import pytest

from forge import budget


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


class TestBuchfuehrung:
    @pytest.fixture
    def db_stub(self, monkeypatch):
        zeilen = {}

        def _execute(sql, params=()):
            schluessel = (params[0], params[1])
            if "erschoepft_seit" in sql:
                # markiere_erschoepft: INSERT ... ON CONFLICT DO UPDATE.
                # Legt bei fehlendem Schluessel eine Zeile mit Nullzaehlern
                # an (wie das echte INSERT es täte) statt wie ein reines
                # UPDATE folgenlos zu verpuffen; bei vorhandener Zeile werden
                # nur erschoepft_seit/grund aktualisiert, die Zähler bleiben
                # unangetastet.
                grund = params[2]
                eintrag = zeilen.setdefault(
                    schluessel, {"nacht": params[0], "provider": params[1],
                                 "laeufe": 0, "tokens_in": 0, "tokens_out": 0,
                                 "erschoepft_seit": None, "grund": None})
                eintrag["erschoepft_seit"] = "jetzt"
                eintrag["grund"] = grund
                return 1
            # buche: INSERT ... ON CONFLICT DO UPDATE, summiert Laeufe/Tokens.
            eintrag = zeilen.setdefault(
                schluessel, {"nacht": params[0], "provider": params[1],
                             "laeufe": 0, "tokens_in": 0, "tokens_out": 0,
                             "erschoepft_seit": None, "grund": None})
            eintrag["laeufe"] += 1
            eintrag["tokens_in"] += params[2]
            eintrag["tokens_out"] += params[3]
            return 1

        def _query_one(sql, params=()):
            return zeilen.get((params[0], params[1]))

        def _query(sql, params=()):
            return list(zeilen.values())

        monkeypatch.setattr(budget.db, "execute", _execute)
        monkeypatch.setattr(budget.db, "query_one", _query_one)
        monkeypatch.setattr(budget.db, "query", _query)
        return zeilen

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
