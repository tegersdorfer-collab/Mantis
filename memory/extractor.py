"""
memory/extractor.py

Auto-Extraktion dauerhafter Fakten + Entitäten/Relationen nach jedem Gesprächswechsel.
Läuft als fire-and-forget Task nach jeder Mantis-Antwort.

Funktionsweise:
  1. LLM analysiert NUR Timos Nachricht (nie Mantis-Antwort → kein Halluzinationsrisiko)
  2. Fakten → LZG (pgvector, mit Jaccard + Vektor-Dedup)
  3. Entitäten + Relationen → Knowledge Graph (PostgreSQL relational)
  4. Regex-Fallback für offensichtliche Fakten wenn LLM nichts liefert

Scope: bewusst nur der LETZTE User-Turn.
"""
import asyncio
import logging
import re

from core.jsonutil import extract_json
from core.llm_gate import GATE
from core.backends.factory import cloud_chain
from core.backends.llm_adapter import BackendLLMAdapter
from memory.lzg import LZG
from memory.knowledge import KnowledgeGraph

log = logging.getLogger(__name__)


def verify_prompt(user_text: str, claim: str) -> str:
    """Verifier-Prompt. Der Sprecher MUSS genannt sein: Der Text ist in Ich-Form,
    die Behauptung sagt „Timo" — das macht die Zuordnung eindeutig."""
    return (
        f"Antworte NUR mit JA oder NEIN, nichts anderes.\n\n"
        f"Der Text stammt von Timo und ist in der Ich-Form geschrieben (\"ich\" = Timo).\n"
        f"Text: \"{user_text[:500]}\"\n"
        f"Behauptung: \"{claim}\"\n\n"
        f"Wird die Behauptung im Text wörtlich oder eindeutig direkt erwähnt?"
    )


async def verify_claim(provider, user_text: str, claim: str) -> bool:
    """Prüft die Behauptung mit der Fast-Cloud-Kette; Fehler lehnen sie ab."""
    try:
        answer = await provider.complete(
            verify_prompt(user_text, claim), temperature=0.0, max_tokens=8,
        )
    except Exception as e:
        log.debug("Claim-Verifier fehlgeschlagen: %s", e)
        return False
    return answer.strip().upper().startswith(("JA", "YES"))


def make_judge(provider):
    """Konflikt-Judge über dieselbe Fast-Cloud-Kette wie die Extraktion."""
    from memory import conflict
    return conflict.make_llm_judge(provider)


# ── Extraktions-Prompt ────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
Du bist ein Gedächtnis-Extraktor für einen persönlichen AI-Assistenten.
Analysiere den folgenden Gesprächswechsel zwischen Timo und Mantis.

Extrahiere NUR dauerhaft nützliche Fakten über Timo – Dinge, die in \
Wochen oder Monaten noch relevant sind.

Gute Beispiele: Name, Wohnort, Job, Hobbys, Langzeit-Projekte, \
Gewohnheiten, Ernährung, feste Präferenzen, Personen die Timo explizit erwähnt hat.

Schlechte Beispiele: Was er gerade gefragt hat, heutige Stimmung, \
temporäre Pläne, allgemeine Aussagen.

STRENGE Regeln:
- MAX 2 Fakten – nur die wichtigsten
- NUR Fakten die Timo in DIESER Nachricht wörtlich oder eindeutig gesagt hat
- NIEMALS etwas hinzufügen das Timo nicht explizit erwähnt hat
- KEINE Diagnosen, Erklärungen oder Schlussfolgerungen erfinden
- Kurze, präzise Sätze (max 15 Wörter)
- Wenn nichts eindeutig Dauerhaftes: leeres Array []
- Kategorien: identity | preference | fact | goal | pattern | correction

Antwort NUR als JSON-Array:
[{"text": "Timo lebt in München", "category": "identity", "confidence": 0.9}]

Gesprächswechsel:
"""

# ── Regex-Fallback (Deutsch) ───────────────────────────────────────────────────

_REGEX_RULES: list[tuple[re.Pattern, str, str]] = [
    # Name
    (re.compile(r'\bich heiße\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß -]{1,40})\b', re.I),
     "identity", "Timos Name ist {match}."),
    # Wohnort
    (re.compile(r'\bich wohne (?:in|bei)\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .,-]{2,60}?)(?:\.|,|$)', re.I),
     "identity", "Timo wohnt in {match}."),
    # Job
    (re.compile(r'\bich (?:arbeite|bin tätig) (?:als|bei|für)\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .,-]{2,60}?)(?:\.|,|$)', re.I),
     "identity", "Timo arbeitet als/bei {match}."),
    # Ziel
    (re.compile(r'\bich (?:will|möchte|plane|vorhabe)\s+(.{8,100}?)(?:\.|,|$)', re.I),
     "goal", "Timo will/möchte {match}."),
    # Essens-Präferenz
    (re.compile(r'\bich esse (?:gerne|lieber|am liebsten|viel|kein|keine|wenig)\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß ,]{2,60}?)(?:\.|,|$)', re.I),
     "preference", "Timo isst {match}."),
    # Allgemeine Präferenz
    (re.compile(r'\bich mag (?:am liebsten|sehr|gerne|nicht|kein|keine)\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß ,]{4,80}?)(?:\.|,|$)', re.I),
     "preference", "Timo mag {match}."),
]


def _regex_candidates(text: str) -> list[dict]:
    """Extrahiert offensichtliche Fakten per Regex – kein LLM nötig."""
    results: list[dict] = []
    seen: set[str] = set()
    for pattern, category, template in _REGEX_RULES:
        m = pattern.search(text)
        if not m:
            continue
        fact = template.replace("{match}", m.group(1).strip(" .,!?"))
        key = fact.lower()
        if key in seen or len(fact) > 120:
            continue
        seen.add(key)
        results.append({"text": fact, "category": category, "confidence": 0.75})
    return results[:2]


# ── Jaccard-Textähnlichkeit (billige Dedup-Vorstufe) ─────────────────────────

def _jaccard(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── Haupt-Extraktor ───────────────────────────────────────────────────────────

class MemoryExtractor:
    """
    Extrahiert nach jedem Gesprächswechsel neue Fakten über Timo
    und speichert sie ins Langzeitgedächtnis (LZG).

    Nutzung:
        extractor = MemoryExtractor(llm_provider, lzg)
        asyncio.create_task(extractor.extract_from_exchange(user_text, response))
    """

    def __init__(
        self,
        llm_provider,
        lzg: LZG,
        kg: KnowledgeGraph | None = None,
        fact_llm=None,
    ):
        self._llm = llm_provider   # nur für embed()
        self._lzg = lzg
        self._kg  = kg
        self._fact_llm = fact_llm or BackendLLMAdapter(
            cloud_chain("fast", reasoning="low", timeout=8),
        )

    async def _ask_json(self, prompt: str, max_tokens: int, gate_timeout_s: float) -> str | None:
        """Holt JSON aus dem Cloud-Fast-Purpose, begrenzt durch den LLM-Gate."""
        try:
            await asyncio.wait_for(GATE.acquire(), timeout=gate_timeout_s)
        except asyncio.TimeoutError:
            return None
        try:
            return await self._fact_llm.complete(
                prompt, temperature=0.1, max_tokens=max_tokens, format="json",
            )
        finally:
            GATE.release()

    async def extract_from_exchange(
        self,
        user_text: str,
        assistant_text: str,
    ) -> int:
        """Öffentlicher Einstiegspunkt. Fehler werden geloggt, nie weiter geworfen."""
        try:
            return await self._run(user_text, assistant_text)
        except Exception as e:
            log.debug(f"MemoryExtractor unerwarteter Fehler: {e}", exc_info=True)
            return 0

    async def _run(self, user_text: str, assistant_text: str) -> int:
        # Nur Timos eigene Worte als Faktenquelle – Mantis-Antworten nie extrahieren
        # (verhindert dass halluzinierte Inhalte aus Mantis-Antworten ins Gedächtnis kommen)
        exchange = f"Timo sagt: {user_text}"

        # ── 1. LLM-Extraktion (Fast-Purpose) ──────────────────────────────
        facts: list[dict] = []
        try:
            raw = await self._ask_json(_EXTRACT_PROMPT + exchange, 300, 8.0)
            if raw is None:
                log.debug("Extraktor: GATE-Timeout, überspringe diesen Turn")
                return 0
            parsed = extract_json(raw, default=[])
            if isinstance(parsed, list):
                facts = [f for f in parsed if isinstance(f, dict)]
        except Exception as e:
            log.debug(f"LLM-Extraktion fehlgeschlagen, Regex-Fallback aktiv: {e}")

        # ── 2. Regex-Fallback ──────────────────────────────────────────────
        fallback = _regex_candidates(user_text)
        # Fallback nur nutzen wenn LLM nichts geliefert hat oder scheiterte
        if not facts and fallback:
            facts = fallback
        elif fallback:
            # Regex-Kandidaten hinzufügen die LLM nicht gefunden hat
            llm_texts = {f.get("text", "").lower() for f in facts}
            for fb in fallback:
                if not any(_jaccard(fb["text"], lt) > 0.5 for lt in llm_texts):
                    facts.append(fb)

        if not facts:
            return 0

        # ── 3. Bestehende Memories für Jaccard-Dedup laden ────────────────
        try:
            existing_memories = await asyncio.to_thread(self._lzg.get_all, limit=300)
        except Exception:
            existing_memories = []
        existing_texts = [m.content for m in existing_memories]

        saved = 0
        for fact in facts:
            text = str(fact.get("text", "")).strip()
            category = str(fact.get("category", "fact"))
            try:
                confidence = min(0.99, float(fact.get("confidence", 0.75)))
            except (ValueError, TypeError):
                confidence = 0.75

            if not text or len(text) < 8:
                continue

            # ── 4. Jaccard Text-Dedup (kein Embedding nötig) ──────────────
            if any(_jaccard(text, ex) >= 0.55 for ex in existing_texts):
                log.debug(f"Extraktor: Text-Duplikat übersprungen: '{text[:60]}'")
                continue

            # ── 4b. Verifier: steht das wirklich im Text? ────────────────────
            try:
                if not await verify_claim(
                    self._fact_llm, user_text, text,
                ):
                    log.debug(f"Extraktor: Verifier abgelehnt: '{text[:60]}'")
                    continue
            except Exception as ve:
                log.debug(f"Verifier fehlgeschlagen, überspringe Check: {ve}")

            # ── 5. Vektor-Dedup via pgvector ──────────────────────────────
            try:
                embedding = await self._llm.embed(text)
                similar = await asyncio.to_thread(
                    self._lzg.find_similar,
                    embedding,
                    0.20,   # cosine distance < 0.20 = semantisch sehr ähnlich
                )
                if similar:
                    best, dist = similar[0]
                    # Bestätigung: Konfidenz leicht erhöhen
                    new_conf = min(0.99, max(best.confidence, confidence) + 0.02)
                    await asyncio.to_thread(
                        self._lzg.update_confidence, best.id, new_conf
                    )
                    log.debug(
                        f"Extraktor: Bestätigt (dist={dist:.3f}): '{best.content[:60]}'"
                    )
                    continue

                # ── 6. Neu speichern ───────────────────────────────────────
                new_id = await asyncio.to_thread(
                    self._lzg.save,
                    text, embedding, category, confidence,
                    {"source": "auto_extract"},
                )
                existing_texts.append(text)   # Intra-Batch-Dedup aktualisieren
                saved += 1
                log.info(f"🧠 Neues Gedächtnis [{category}]: {text}")

                # ── 6b. Konfliktauflösung: überholt der neue Fakt einen alten? ──
                # (z.B. Umzug → alter Wohnort abwerten). Guarded: darf die
                # Extraktion nie kippen.
                try:
                    from memory import conflict
                    judge = make_judge(self._fact_llm)
                    await conflict.resolve(self._lzg, new_id, text, embedding, judge)
                except Exception as e:
                    log.debug(f"Konfliktauflösung übersprungen: {e}")

            except Exception as e:
                log.debug(f"Extraktor Vektor-Schritt fehlgeschlagen: {e}")

        # ── 7. Entity-Extraktion für Knowledge Graph ──────────────────────
        if self._kg and saved > 0:
            await self._extract_entities(user_text)

        return saved

    async def _extract_entities(self, user_text: str) -> None:
        """Extrahiert Personen/Orte/Projekte aus Timos Nachricht und speichert sie im KG."""
        _KG_PROMPT = """\
Analysiere diese Nachricht von Timo und extrahiere Entitäten und Relationen.

Entitäts-Typen: person | place | project | organization | concept
Relationen (Prädikate): ist_schwester_von | ist_bruder_von | ist_freund_von |
  ist_partner_von | ist_mutter_von | ist_vater_von | ist_kollege_von |
  wohnt_in | studiert_in | arbeitet_bei | arbeitet_an | besitzt | plant

Timo ist immer das Subjekt oder Objekt – er ist bereits bekannt, taucht nicht als Entität auf.

Antworte NUR als JSON-Array (leer wenn nichts gefunden):
[{"name": "Anna", "type": "person", "predicate": "ist_schwester_von", "direction": "object"}]
direction: "subject" = Timo ist Subjekt (Timo → predicate → entity)
           "object"  = Timo ist Objekt  (entity → predicate → Timo)

Nachricht: """ + user_text + "\n\nJSON:"

        try:
            raw = await self._ask_json(_KG_PROMPT, 200, 6.0)
            if raw is None:
                return
        except Exception as e:
            log.debug(f"KG-Entity-Extraktion fehlgeschlagen: {e}")
            return
        entries = extract_json(raw, default=[])
        if not isinstance(entries, list):
            return

        # Timo als zentrale Entität sicherstellen
        timo_id = await asyncio.to_thread(
            self._kg.upsert_entity, "Timo", "person", "Besitzer / Hauptnutzer"
        )

        for e in entries:
            if not isinstance(e, dict):
                continue
            name      = str(e.get("name", "")).strip()
            etype     = str(e.get("type", "person"))
            predicate = str(e.get("predicate", "bekannt_mit"))
            direction = str(e.get("direction", "subject"))
            if not name:
                continue
            try:
                eid = await asyncio.to_thread(self._kg.upsert_entity, name, etype)
                if direction == "subject":
                    await asyncio.to_thread(
                        self._kg.upsert_relation, timo_id, predicate, eid,
                        user_text[:200], 0.85, "extract"
                    )
                else:
                    await asyncio.to_thread(
                        self._kg.upsert_relation, eid, predicate, timo_id,
                        user_text[:200], 0.85, "extract"
                    )
                log.info(f"🕸️ KG: {name} ({etype}) via '{predicate}'")
            except Exception as ex:
                log.debug(f"KG-Entity fehlgeschlagen: {ex}")
