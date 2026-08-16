"""Holt die COROS-Daten und schreibt sie nach health_data.

Ein Aufruf je Tool, alles je Tag zusammengeführt, dann ein Upsert pro Tag. Kein
gefensterter Backfill und kein Wasserstand: die gesamte Historie passt in einen
Aufruf (Spec-Nachtrag 2026-08-16), ein misslungener Lauf wird einfach wiederholt.

Ein einzelnes kaputtes Tool kostet nur seine eigenen Felder — Teildaten sind
besser als keine.
"""
import logging
from datetime import date, timedelta

from core import db
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
#
# Die Reihenfolge entscheidet: spätere Quellen überschreiben frühere (dict.update).
# querySleepData steht deshalb VOR queryDailyHealthData — es rechnet die Phasen aus
# Prozentanteilen und deckt dafür die ganze Historie ab, während queryDailyHealthData
# exakte Dauern liefert, aber nur für die jüngsten Tage. So füllt die gerechnete
# Quelle die Lücken und die exakte gewinnt, wo es beide gibt.
_DAY_SOURCES = [
    ("querySleepData",              mapping.parse_sleep,         True),
    ("queryDailyHealthData",        mapping.parse_daily_health,  False),
    ("querySleepHrv",               mapping.parse_sleep_hrv,     True),
    ("queryRestingHeartRate",       mapping.parse_resting_hr,    False),
    ("queryAvgHeartRate",           mapping.parse_avg_hr,        False),
    ("queryTrainingLoadAssessment", mapping.parse_training_load, False),
]

# Tageslos, aber ein echter Tageswert → jüngster erfasster Tag in health_data.
_FLAT_SOURCES = [
    ("queryUserInfo", mapping.parse_user_info),
]

# Momentaufnahmen ohne Verlauf → health_assessment. Eigene Tabelle, weil sie als
# Spalten in health_data auf genau einer von hunderten Zeilen stünden und damit
# jede Abfrage und jeden Coverage-Wert im Scoring verwässern würden.
_ASSESSMENT_SOURCES = [
    ("queryFitnessAssessmentOverview", mapping.parse_fitness_overview),
    ("queryRecoveryStatus",            mapping.parse_recovery),
]

# Aus queryUserInfo, aber konstant — gehört nicht in eine Tages-Tabelle.
_PROFILE_KEYS = ("height_cm", "birthday", "gender", "nickname")


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


def collect_all(client, days: int) -> tuple[dict[str, dict], dict, dict]:
    """Alle Quellen abfragen und nach Zielort sortiert zurückgeben.

    Liefert (Tageswerte, Momentaufnahme, Profil): health_data, health_assessment
    und settings. Die Rohantworten werden je Tool nur einmal geholt — die
    Kopfzeile von queryDailyHealthData trägt Momentanwerte, der Rumpf Tageswerte.
    """
    responses: dict[str, str] = {}

    def fetch(tool: str, args: dict) -> str:
        if tool not in responses:
            responses[tool] = str(payload(client.call_tool(tool, args)))
        return responses[tool]

    merged: dict[str, dict] = {}
    for tool, parse, needs_range in _DAY_SOURCES:
        try:
            parsed = parse(fetch(tool, _args(days, needs_range)))
            for day, fields in parsed.items():
                merged.setdefault(day, {}).update(fields)
            # Eine Zeile je Quelle, auch bei 0 Tagen — sonst ist ein degradierter
            # Sync (Struktur erkannt, aber nichts extrahiert) nur an der leeren
            # DB ablesbar, nicht am Log.
            log.info(f"COROS: {tool} lieferte {len(parsed)} Tage")
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    assessment: dict = {}
    profile: dict = {}
    if not merged:
        return merged, assessment, profile

    newest = max(merged)
    for tool, parse in _FLAT_SOURCES:
        try:
            fields = parse(fetch(tool, {}))
            profile.update({k: fields.pop(k) for k in _PROFILE_KEYS if k in fields})
            merged[newest].update(fields)
            log.info(f"COROS: {tool} lieferte {len(fields)} Felder")
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    for tool, parse in _ASSESSMENT_SOURCES:
        try:
            fields = parse(fetch(tool, {}))
            assessment.update(fields)
            log.info(f"COROS: {tool} lieferte {len(fields)} Felder")
        except Exception as e:
            log.warning(f"COROS: {tool} übersprungen ({type(e).__name__}: {e})")

    # Die Kopfzeile der Tagesantwort gilt für den ganzen Abruf, nicht für einen Tag.
    if text := responses.get("queryDailyHealthData"):
        try:
            assessment.update(mapping.parse_daily_header(text))
        except Exception as e:
            log.warning(f"COROS: Kopfzeile übersprungen ({type(e).__name__}: {e})")

    return merged, assessment, profile


def collect(client, days: int) -> dict[str, dict]:
    """Nur die Tageswerte — schmale Sicht auf collect_all() für Aufrufer und Tests."""
    return collect_all(client, days)[0]


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
        days_data, assessment, profile = collect_all(c, days)
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

    # Momentaufnahme und Profil je in einem eigenen try: ein Fehler dort darf die
    # bereits geschriebenen Tageswerte nicht entwerten — sie sind der Hauptzweck.
    if assessment and days_data:
        try:
            health.upsert_assessment(max(days_data), assessment)
        except Exception as e:
            log.warning(f"COROS: Assessment nicht geschrieben ({type(e).__name__}: {e})")
    if profile:
        try:
            for key, value in profile.items():
                db.set_setting(f"coros_{key}", value)
        except Exception as e:
            log.warning(f"COROS: Profil nicht gespeichert ({type(e).__name__}: {e})")

    if written:
        log.info(f"🩺 COROS: {written} Tage geschrieben")
    return written
