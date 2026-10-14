"""
Journal — API-Router. Aus web/api.py extrahiert (verhaltensgleich).
Endpoints sind Closures über `orch`; build_router(orch) liefert den APIRouter.
"""
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request

from core import db
from domains import journal

from web.routers._helpers import _jsonable

log = logging.getLogger("mantis.api")
WEB_DIR = Path(__file__).parent.parent


def build_router(orch=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/journal")
    def get_journal(limit: int = 30):
        return _jsonable(journal.recent_entries(limit))

    @router.get("/api/journal/today")
    def get_journal_today():
        return _jsonable(journal.today_entry()) or {}

    @router.get("/api/journal/prompts")
    async def get_journal_prompts():
        """Generiert 3 personalisierte Journalfragen per LLM."""
        if not orch:
            return {"prompts": ["Wie war dein Tag?", "Was hat dich beschäftigt?", "Worauf bist du stolz?"]}
        # Kontext sammeln
        recent = journal.recent_entries(5)
        moods = journal.mood_trend(7)
        ctx_lines = []
        if recent:
            ctx_lines.append("Letzte Einträge (Themen): " + "; ".join(
                e.get("content", "")[:60] for e in recent if e.get("content")
            ))
        if moods:
            avg_mood = sum(m["mood"] for m in moods if m.get("mood")) / max(len(moods), 1)
            ctx_lines.append(f"Durchschnittliche Stimmung letzte 7 Tage: {avg_mood:.1f}/5")
        ctx = "\n".join(ctx_lines) or "Keine bisherigen Einträge."
        prompt = (
            f"Du bist Mantis, ein persönlicher AI-Begleiter. Generiere genau 3 kurze, "
            f"persönliche Journalfragen für den Abend-Check-in von Timo. "
            f"Die Fragen sollen zur Selbstreflexion anregen, variieren und nicht zu allgemein sein. "
            f"Kontext:\n{ctx}\n\n"
            f"Antworte NUR mit den 3 Fragen, eine pro Zeile, ohne Nummerierung oder Formatierung."
        )
        resp = await orch.bg_llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9, max_tokens=200,
        )
        lines = [l.strip() for l in resp.strip().splitlines() if l.strip()][:3]
        while len(lines) < 3:
            lines.append("Was nimmst du aus dem heutigen Tag mit?")
        return {"prompts": lines}

    @router.post("/api/journal")
    async def add_journal(req: Request):
        d = await req.json()
        jid = journal.add_entry(
            content=d.get("content", ""),
            mood=d.get("mood"),
            energy=d.get("energy"),
            tags=d.get("tags"),
            prompts_answers=d.get("prompts_answers"),
        )
        # Memory-Extraktion im Hintergrund
        if orch and (d.get("content") or d.get("prompts_answers")):
            import asyncio as _aio
            parts = []
            if d.get("prompts_answers"):
                for qa in d["prompts_answers"]:
                    if qa.get("answer"):
                        parts.append(f"Frage: {qa['question']}\nAntwort: {qa['answer']}")
            if d.get("content"):
                parts.append(f"Freier Text: {d['content']}")
            combined = "\n\n".join(parts)
            _aio.create_task(orch.extractor.extract_from_exchange(
                f"Journal-Eintrag vom {date.today()}:\n{combined}", ""
            ))
        return {"id": jid}

    @router.get("/api/journal/mood")
    def mood(days: int = 30):
        return _jsonable(journal.mood_trend(days))

    @router.get("/api/journal/themes")
    def journal_themes(days: int = 90):
        rows = db.query(
            "SELECT content, prompts_answers FROM journal_entries "
            "WHERE date >= CURRENT_DATE - %s",
            (days,),
        )
        stopwords = {
            "ich", "und", "der", "die", "das", "den", "dem", "des", "ein", "eine",
            "einen", "einem", "einer", "ist", "war", "bin", "war", "auch", "noch",
            "aber", "nicht", "mit", "von", "für", "auf", "habe", "hat", "hatte",
            "sehr", "mehr", "heute", "mich", "mir", "mein", "meine", "wie", "was",
            "dass", "sich", "sind", "wird", "werden", "kann", "könnte", "soll",
            "über", "unter", "nach", "schon", "etwas", "alle", "alles", "nur",
            "dann", "doch", "wenn", "weil", "diese", "dieser", "dieses", "dabei",
        }
        from collections import Counter
        import re as _re
        counter = Counter()
        for r in rows:
            text = r.get("content") or ""
            pa = r.get("prompts_answers")
            if pa:
                items = pa if isinstance(pa, list) else []
                text += " " + " ".join(i.get("answer", "") for i in items if isinstance(i, dict))
            words = _re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", text.lower())
            counter.update(w for w in words if w not in stopwords)
        return [{"word": w, "count": c} for w, c in counter.most_common(40)]

    return router
