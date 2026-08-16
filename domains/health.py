"""
Health-Domäne (mantis-nativ).
Lese- und Schreibzugriff der Domäne auf health_data. Befüllt wird die Tabelle
von domains/coros/importer.py (COROS-MCP-Sync); diese Datei kennt die
Datenquelle nicht, nur die Tabelle.
"""
import logging

from core import db

log = logging.getLogger(__name__)


def upsert_day(day: str, fields: dict) -> bool:
    """Einen Tag in health_data schreiben. Formatunabhängig — wer die Felder
    erzeugt hat, ist nicht die Sache dieser Domäne.

    Idempotent über ON CONFLICT; leere Feld-Dicts werden nicht geschrieben.

    Gibt zurück, ob sich dabei etwas geändert hat — nicht ob geschrieben wurde.
    Eine neue Zeile zählt als Änderung; ein Update, das exakt dieselben Werte
    noch einmal schreibt, nicht. Die WHERE-Klausel im ON CONFLICT vergleicht
    jede geschriebene Spalte per IS DISTINCT FROM gegen den Bestand — matcht
    keine, überspringt Postgres das UPDATE komplett und rowcount bleibt 0.
    Ohne das würde jeder 30-Minuten-Tick als 'neue Daten' durchgehen, selbst
    wenn COROS denselben Tag unverändert zurückliefert.
    """
    if not day or not fields:
        return False
    cols = list(fields.keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    changed = " OR ".join(f"health_data.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO health_data (date, {', '.join(cols)}, updated_at) "
        f"VALUES (%s, {', '.join(['%s'] * len(cols))}, NOW()) "
        f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW() "
        f"WHERE {changed}"
    )
    rowcount = db.execute(sql, tuple([day] + [fields[c] for c in cols]))
    return rowcount > 0


def upsert_assessment(day: str, fields: dict) -> bool:
    """Eine Momentaufnahme in health_assessment schreiben.

    Getrennt von upsert_day, weil die Werte hier keinen Tagesbezug haben: COROS
    liefert VO2max, Erholung und Renn-Prognosen als aktuellen Stand ohne Verlauf.
    Abgelegt wird unter dem Tag des Abrufs, damit über die Zeit trotzdem eine
    Reihe entsteht. Gleiche Idempotenz-Regel wie upsert_day.
    """
    if not day or not fields:
        return False
    cols = list(fields.keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    changed = " OR ".join(f"health_assessment.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO health_assessment (date, {', '.join(cols)}, updated_at) "
        f"VALUES (%s, {', '.join(['%s'] * len(cols))}, NOW()) "
        f"ON CONFLICT (date) DO UPDATE SET {updates}, updated_at=NOW() "
        f"WHERE {changed}"
    )
    return db.execute(sql, tuple([day] + [fields[c] for c in cols])) > 0


def recent(days: int = 7) -> list[dict]:
    return db.query(
        "SELECT * FROM health_data WHERE date >= CURRENT_DATE - %s ORDER BY date DESC",
        (days,),
    )


def latest() -> dict | None:
    return db.query_one("SELECT * FROM health_data ORDER BY date DESC LIMIT 1")


def history(days: int = 30) -> list[dict]:
    return db.query(
        "SELECT * FROM health_data WHERE date >= CURRENT_DATE - %s ORDER BY date",
        (days,),
    )
