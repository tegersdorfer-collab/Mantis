"""Holt die COROS-Daten und schreibt sie nach health_data.

Ein Aufruf je Tool, alles je Tag zusammengeführt, dann ein Upsert pro Tag. Kein
gefensterter Backfill und kein Wasserstand: die gesamte Historie passt in einen
Aufruf (Spec-Nachtrag 2026-08-16), ein misslungener Lauf wird einfach wiederholt.

Ein einzelnes kaputtes Tool kostet nur seine eigenen Felder — Teildaten sind
besser als keine.
"""
import logging
from datetime import date, timedelta

from domains import health
from domains.coros import mapping, oauth
from domains.coros.client import CorosClient, payload

log = logging.getLogger("mantis.coros")

# Live gegen die COROS-API gemessen (2026-08-16): queryRestingHeartRate,
# queryAvgHeartRate, queryStressLevel und querySleepHrv nehmen 365 Tage
# klaglos an, irgendwo zwischen 365 und 1000 fangen sie an, mit einem
# Fehlertext zu antworten. queryDailyHealthData und querySleepData vertragen
# auch 1000, queryTrainingLoadAssessment deckelt sich selbst bei 31 Tagen —
# aber ein einziger, für alle Tools gültiger Wert ist einfacher als je Tool
# einer, und die gesamte Historie des Kontos umfasst nur 164 Tage. 365 ist
# also reichlich Puffer, ohne je an die tatsächliche Grenze zu stoßen.
MAX_LOOKBACK_DAYS = 365

# (Tool, Parser, braucht Datumsbereich) — tageslose Parser stehen weiter unten.
_DAY_SOURCES = [
    ("queryDailyHealthData",        mapping.parse_daily_health,  False),
    ("querySleepData",              mapping.parse_sleep,         True),
    ("querySleepHrv",               mapping.parse_sleep_hrv,     True),
    ("queryRestingHeartRate",       mapping.parse_resting_hr,    False),
    ("queryAvgHeartRate",           mapping.parse_avg_hr,        False),
    ("queryTrainingLoadAssessment", mapping.parse_training_load, False),
]

_FLAT_SOURCES = [
    ("queryFitnessAssessmentOverview", mapping.parse_fitness_overview),
    ("queryRecoveryStatus",            mapping.parse_recovery),
]


def _build_client() -> CorosClient:
    oauth.access_token()          # früh scheitern, wenn nicht autorisiert
    return CorosClient()


def _args(days: int, needs_range: bool) -> dict:
    """COROS will yyyyMMdd, und manche Tools nur einen Rückblick-Zähler.

    Der Rückblick wird auf MAX_LOOKBACK_DAYS gedeckelt — größere Werte
    beantworten manche Tools mit einem Fehlertext statt mit Daten.
    """
    days = min(days, MAX_LOOKBACK_DAYS)
    if not needs_range:
        return {"days": days}
    end = date.today()
    start = end - timedelta(days=days)
    return {"startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "days": days}


def collect(client, days: int) -> dict[str, dict]:
    """Alle Quellen abfragen und je Tag zusammenführen."""
    merged: dict[str, dict] = {}

    for tool, parse, needs_range in _DAY_SOURCES:
        try:
            text = payload(client.call_tool(tool, _args(days, needs_range)))
            for day, fields in parse(str(text)).items():
                merged.setdefault(day, {}).update(fields)
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    if not merged:
        return merged

    # Tageslose Metriken beschreiben den aktuellen Stand → jüngster erfasster Tag.
    newest = max(merged)
    for tool, parse in _FLAT_SOURCES:
        try:
            text = payload(client.call_tool(tool, {}))
            merged[newest].update(parse(str(text)))
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    return merged


def sync(days: int = 14, client=None) -> int:
    """Die letzten `days` Tage holen und schreiben. Gibt die Anzahl Tage zurück.

    Degradiert freundlich: fehlendes Token, fehlendes Netz und ein Schreibfehler
    ergeben je eine Logzeile und einen Rückgabewert, nie eine Ausnahme — der
    Aufrufer ist ein Hintergrund-Tick.
    """
    owns_client = client is None
    try:
        c = client or _build_client()
    except oauth.CorosNotAuthorized as e:
        log.info(f"COROS nicht autorisiert ({e}) — `python3 scripts/coros_auth.py` ausführen")
        return 0
    except Exception as e:
        log.warning(f"COROS-Client nicht aufgebaut: {e}")
        return 0

    try:
        days_data = collect(c, days)
    except Exception as e:
        log.warning(f"COROS-Sync fehlgeschlagen: {e}")
        return 0
    finally:
        # Nur schließen, was sync() selbst gebaut hat — ein injizierter Client
        # (Tests, manuelle Läufe) gehört dem Aufrufer.
        if owns_client:
            c.close()

    # Pro Tag einzeln fangen statt die gesamte Schleife in ein try zu packen:
    # ein einzelner kaputter Tag darf nicht alle folgenden mitreißen (dieselbe
    # Leitlinie wie in collect()). Der Fehlerzähler landet in einer Logzeile,
    # nicht in einer je Tag — eine tote DB darf nicht 94 Warnungen erzeugen.
    written = 0
    failed = 0
    last_error: Exception | None = None
    for day, fields in sorted(days_data.items()):
        try:
            if health.upsert_day(day, fields):
                written += 1
        except Exception as e:
            failed += 1
            last_error = e

    if failed:
        log.warning(f"COROS-Sync: {failed} von {len(days_data)} Tagen nicht geschrieben "
                    f"(zuletzt: {last_error})")
    if written:
        log.info(f"🩺 COROS: {written} Tage geschrieben")
    return written
