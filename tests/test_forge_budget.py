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
