"""Buchführung über die Gratis-Kontingente, je Nacht und Anbieter.

**Was diese Zahlen sind und was nicht.** Die Anbieter melden ihren Reststand
nicht, und die Forge ruft die `opencode`-CLI auf statt der HTTP-API — Header
wie `x-ratelimit-remaining-*` kommen hier gar nicht an. Die Zähler sind
deshalb eine vorsorgliche Bremse, keine Buchhaltung mit Anspruch auf
Genauigkeit. Wer sie für exakt hält, plant falsch.

Das verlässliche Signal ist die Reaktion, nicht die Vorhersage: meldet ein
Anbieter ein Rate-Limit (RunResult.rate_limited aus forge/runner*.py), gilt er
für den Rest der Nacht als erschöpft. Die Obergrenzen unten greifen nur, wenn
diese Meldung ausbleibt.

Erschöpfung gilt immer für den ganzen Anbieter, nie nur für ein Modell: läuft
NVIDIAs Kontingent leer, hilft es nicht, dort ein anderes Modell zu probieren.
"""
import logging
from datetime import date, datetime, timedelta

from core import db

log = logging.getLogger(__name__)

# Stunde, vor der ein Lauf noch zur Nacht des Vortags gezählt wird. Ein Lauf um
# 02:00 gehört zur Nacht, die um 23:00 begann — sonst bekäme er um Mitternacht
# ein frisches Kontingent geschenkt, das es nicht gibt.
_NACHT_GRENZE_STUNDE = 12

# Läufe je Anbieter und Nacht, ab denen vorsorglich Schluss ist. Bewusst
# konservativ: für Antigravity sind ~20 Anfragen/Tag eine Community-Angabe,
# keine dokumentierte Zusage — 18 lässt Luft, statt den Account zu riskieren.
# NVIDIA und Google sind undokumentiert; die Zahlen sind Schätzungen und
# dürfen korrigiert werden, sobald jemand echte misst.
OBERGRENZEN = {
    "antigravity": 18,
    "nvidia": 120,
    "google": 200,
    "groq": 400,
}


def provider_von_modell(model: str) -> str:
    """Welcher Anbieter steckt hinter einer Modellkennung?

    opencode-Modelle tragen den Anbieter als erstes Pfadsegment
    ("nvidia/moonshotai/kimi-k3"). agy-Modelle haben kein solches Präfix
    ("claude-opus-4-6-thinking") und laufen alle über Antigravity.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    return "antigravity"


def nacht_id(jetzt: datetime | None = None) -> date:
    """Der Schlüssel der laufenden Nacht."""
    jetzt = jetzt or datetime.now()
    if jetzt.hour < _NACHT_GRENZE_STUNDE:
        return (jetzt - timedelta(days=1)).date()
    return jetzt.date()


def buche(model: str, tokens_in: int, tokens_out: int) -> None:
    """Zählt einen Lauf und seine Tokens auf den Anbieter des Modells."""
    provider = provider_von_modell(model)
    db.execute(
        "INSERT INTO forge_budget (nacht, provider, laeufe, tokens_in, tokens_out) "
        "VALUES (%s, %s, 1, %s, %s) "
        "ON CONFLICT (nacht, provider) DO UPDATE SET "
        "laeufe = forge_budget.laeufe + 1, "
        "tokens_in = forge_budget.tokens_in + EXCLUDED.tokens_in, "
        "tokens_out = forge_budget.tokens_out + EXCLUDED.tokens_out",
        (nacht_id(), provider, tokens_in, tokens_out),
    )


def markiere_erschoepft(model: str, grund: str) -> None:
    """Der Anbieter ist für den Rest der Nacht raus."""
    provider = provider_von_modell(model)
    log.warning(f"Forge-Budget: {provider} erschöpft — {grund}")
    db.execute(
        "UPDATE forge_budget SET erschoepft_seit = NOW(), grund = %s "
        "WHERE nacht = %s AND provider = %s",
        (grund, nacht_id(), provider),
    )


def ist_erschoepft(model: str) -> bool:
    """Darf dieses Modell heute Nacht noch benutzt werden?"""
    provider = provider_von_modell(model)
    zeile = db.query_one(
        "SELECT laeufe, erschoepft_seit FROM forge_budget WHERE nacht = %s AND provider = %s",
        (nacht_id(), provider),
    )
    if not zeile:
        return False
    if zeile.get("erschoepft_seit"):
        return True
    grenze = OBERGRENZEN.get(provider)
    return grenze is not None and (zeile.get("laeufe") or 0) >= grenze


def stand() -> list[dict]:
    """Alle Anbieter dieser Nacht — für Bericht und Dashboard."""
    return db.query(
        "SELECT * FROM forge_budget WHERE nacht = %s ORDER BY provider",
        (nacht_id(),),
    )
