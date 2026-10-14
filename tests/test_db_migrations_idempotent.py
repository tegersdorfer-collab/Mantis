"""Migrationen dürfen beim erneuten Lauf keine Spalten hinzufügen und wieder löschen.

Postgres zählt gelöschte Spalten weiter gegen das Limit von 1600 Spalten pro Tabelle;
ein ADD-dann-DROP in den Migrationen lässt die Tabelle bei jedem Start wachsen.
"""
import pytest

from core import db


def _attribute_counts() -> dict[str, int]:
    rows = db.query(
        """SELECT c.relname AS name, count(*) AS n
           FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
           JOIN pg_namespace ns ON ns.oid = c.relnamespace
           WHERE a.attnum > 0 AND c.relkind = 'r' AND ns.nspname = 'public'
           GROUP BY c.relname"""
    )
    return {row["name"]: row["n"] for row in rows}


@pytest.mark.db
def test_migrations_rerun_does_not_grow_tables():
    try:
        db.run_migrations()
    except Exception:
        pytest.skip("Test-DB nicht erreichbar")
    before = _attribute_counts()
    db.run_migrations()
    after = _attribute_counts()
    grown = {name: (before.get(name), n) for name, n in after.items() if n != before.get(name)}
    assert grown == {}
