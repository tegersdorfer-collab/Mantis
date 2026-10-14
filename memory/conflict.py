"""Memory-Konfliktauflösung.

Das LZG ist ADD-only (bewusst, gegen stale-update-Bugs). Ohne Auflösung häufen
sich dadurch widersprüchliche Fakten an ("wohnt in München" + später "wohnt in
Berlin" koexistieren). Dieses Modul findet ähnliche, aber NICHT identische
Bestandsfakten und lässt einen (injizierbaren) Judge entscheiden, ob sie vom
neuen Fakt überholt werden. Überholte werden abgewertet (`lzg.supersede`), nicht
gelöscht — der neue Fakt gewinnt im Recall, die Historie bleibt erhalten.

Der Judge ist injizierbar, damit die Logik ohne LLM testbar ist; im Betrieb ist
es ein kleiner, strenger Ja/Nein-Modell-Call.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# Distanz-Band (cosine): nah genug fürs selbe Thema, aber nicht ~identisch.
# Fast identische Fakten behandelt der Extraktor als BESTÄTIGUNG (Konfidenz-Bump),
# nicht als Konflikt — deshalb die Untergrenze.
CONFLICT_MIN_DIST = 0.12
CONFLICT_MAX_DIST = 0.35

Judge = Callable[[str, str], Awaitable[bool]]  # (alt, neu) -> "alt ist überholt?"


async def resolve(lzg, new_id: int, new_text: str, new_embedding,
                  judge: Judge, max_candidates: int = 5) -> int:
    """Wertet vom neuen Fakt überholte Bestandsfakten ab. Gibt deren Anzahl zurück.

    Fehler (Suche/Judge/Update) werden geschluckt — Konfliktauflösung darf die
    Gedächtnis-Extraktion nie kippen.
    """
    try:
        candidates = await asyncio.to_thread(
            lzg.find_similar, new_embedding, CONFLICT_MAX_DIST, max_candidates + 1
        )
    except Exception as e:
        log.debug(f"Konflikt-Suche fehlgeschlagen: {e}")
        return 0

    superseded = 0
    for mem, dist in candidates:
        if mem.id == new_id:
            continue
        if dist < CONFLICT_MIN_DIST:
            continue  # ~identisch → Bestätigung, kein Konflikt
        try:
            if await judge(mem.content, new_text):
                await asyncio.to_thread(lzg.supersede, mem.id, new_id)
                superseded += 1
                log.info(f"🔄 Fakt überholt: '{mem.content[:50]}' ← '{new_text[:50]}'")
        except Exception as e:
            log.debug(f"Konflikt-Judge fehlgeschlagen: {e}")
    return superseded


# ── LLM-Judge fürs Produktivsystem ────────────────────────────────────────────

_JUDGE_PROMPT = (
    "Alte Aussage über Timo: \"{old}\"\n"
    "Neue Aussage über Timo: \"{new}\"\n\n"
    "Macht die NEUE Aussage die ALTE veraltet oder widerspricht ihr direkt "
    "(z.B. Umzug, Jobwechsel, geänderte Präferenz, geänderter Status)? "
    "Zwei Dinge, die gleichzeitig wahr sein können, sind KEIN Widerspruch. "
    "Antworte NUR mit JA oder NEIN."
)


def make_llm_judge(llm_provider) -> Judge:
    """Baut einen strengen Cloud-Judge (bei Fehlern gilt konservativ NEIN)."""
    async def _judge(old: str, new: str) -> bool:
        try:
            answer = await llm_provider.complete(
                _JUDGE_PROMPT.format(old=old, new=new),
                temperature=0.0,
                max_tokens=8,
            )
            return answer.strip().upper().startswith(("JA", "YES"))
        except Exception:
            return False  # im Zweifel NICHT abwerten
    return _judge
