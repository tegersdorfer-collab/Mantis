"""
Background Review Loop — nach jedem Turn läuft ein Fork-Agent der entscheidet
ob ein neues Skill/Memory gespeichert werden soll.

Inspiriert von Hermes Agent's background_review.py:
- Non-blocking: Hauptkonversation wartet nicht
- Nur whitelisted Tools: create_skill, save_memory, update_skill_md
- Direktive "BE ACTIVE": die meisten Turns produzieren mindestens ein Update
- Skill-Signale: Technik benutzt, User korrigierte Stil, Muster erkannt
"""
import asyncio
import logging
import re
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

SKILLS_MD_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS_MD_DIR.mkdir(parents=True, exist_ok=True)

_REVIEW_PROMPT = """\
Du bist Mantis' Lern-Agent. Analysiere den letzten Gesprächsaustausch und entscheide,
ob ein Skill oder eine Erinnerung gespeichert werden soll.

DIREKTIVE — SEI AKTIV:
Die meisten Turns sollten ein Update produzieren. Fehler auf der Seite des Speicherns.

WANN EINEN SKILL ANLEGEN (SKILL.md):
- Mantis hat eine nicht-triviale Technik/Prozedur benutzt (mehr als 2 Tool-Schritte)
- Der User hat Alfrends Stil/Ton/Format korrigiert ("kürzer", "andere Reihenfolge" etc.)
- Ein Muster taucht zum zweiten Mal auf (gleiche Anfrage-Klasse)
- Mantis hat eine clevere Kombination von Tools gefunden die wiederverwendbar ist

WANN EINE ERINNERUNG SPEICHERN:
- Neue Präferenz des Users erkannt ("ich mag X", "mach das nie")
- Wichtige Kontextinformation (Projekt, Ziel, Einschränkung)
- User hat etwas bestätigt/korrigiert

WANN NICHTS TUN:
- Reines Smalltalk ohne neue Info
- Einfache Faktenfragen (Wetter, Uhrzeit)
- Nur 1 einfacher Tool-Call ohne Technik dahinter

SKILL.md FORMAT — nur wenn ein Skill sinnvoll ist:
```
SAVE_SKILL: <skill_name>
---
name: <snake_case_name>
description: <was dieser Skill tut, 1 Zeile>
triggers: [<keyword1>, <keyword2>]
---
<Natürlichsprachliche Prozedur die Mantis in der Zukunft folgen soll.
Konkret: Schritt 1, Schritt 2 etc. Referenziere Tool-Namen wenn relevant.>
```

MEMORY FORMAT — nur wenn neue Info:
```
SAVE_MEMORY: <kurze Erinnerung als Fakt, max 1 Satz>
```

Antworte NUR mit SAVE_SKILL:... oder SAVE_MEMORY:... Blöcken, oder dem Wort SKIP.
Keine Erklärungen.
"""


async def run_background_review(
    user_text: str,
    assistant_response: str,
    tools_used: list[str],
    bg_llm,
    lzg,
) -> None:
    """
    Läuft als Daemon nach jedem Turn. Non-blocking.
    bg_llm: das Background-LLM über die Cloud-Kette
    lzg: Long-Term Memory für Save-Memory
    """
    if not config.BACKGROUND_REVIEW_ENABLED:
        return
    t0 = time.time()
    try:
        context = f"""USER: {user_text}

MANTIS ANTWORT: {assistant_response[:600]}

GENUTZTE TOOLS: {', '.join(tools_used) if tools_used else 'keine'}
"""
        messages = [
            {"role": "user", "content": context}
        ]

        result = await bg_llm.chat(
            messages=messages,
            system=_REVIEW_PROMPT,
            temperature=0.3,
            max_tokens=600,
        )

        text = result.strip() if isinstance(result, str) else (result.get("content") or "")

        if not text or text.upper().startswith("SKIP"):
            return

        # Skill speichern
        skill_match = re.search(
            r"SAVE_SKILL:\s*(\w+)\s*\n---\s*\n(.*?)---\s*\n(.*?)(?=SAVE_SKILL:|SAVE_MEMORY:|$)",
            text, re.DOTALL
        )
        if skill_match:
            skill_name = skill_match.group(1).strip()
            frontmatter = skill_match.group(2).strip()
            body = skill_match.group(3).strip()
            _save_skill_md(skill_name, frontmatter, body, user_text)

        # Memory speichern
        for mem_match in re.finditer(r"SAVE_MEMORY:\s*(.+?)(?=SAVE_SKILL:|SAVE_MEMORY:|$)", text, re.DOTALL):
            fact = mem_match.group(1).strip()
            if fact and len(fact) > 5:
                try:
                    emb = await bg_llm.embed(fact)
                    await asyncio.to_thread(
                        lzg.save, fact, emb, "fact", 0.6,
                        {"source": "background_review"},
                    )
                    log.info(f"[BgReview] Memory gespeichert: {fact[:80]}")
                except Exception as e:
                    log.debug(f"[BgReview] Memory-Save fehlgeschlagen: {e}")

        elapsed = time.time() - t0
        log.info(f"[BgReview] Abgeschlossen in {elapsed:.1f}s")

    except Exception as e:
        log.debug(f"[BgReview] Fehler (non-critical): {e}")


def _save_skill_md(name: str, frontmatter: str, body: str, source_turn: str) -> None:
    """Speichert einen SKILL.md — überschreibt falls schon vorhanden (Update-Logik)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,60}", name):
        log.debug(f"[BgReview] Ungültiger Skill-Name: {name}")
        return

    path = SKILLS_MD_DIR / f"{name}.md"
    already_exists = path.exists()

    content = f"---\n{frontmatter}\ncreated_by: background_review\n---\n\n{body}\n"

    try:
        path.write_text(content, encoding="utf-8")
        action = "aktualisiert" if already_exists else "erstellt"
        log.info(f"[BgReview] Skill.md '{name}' {action}")
        # Sofort in aktiven Index laden
        from core.skill_md import reload_skill
        reload_skill(name)
    except Exception as e:
        log.warning(f"[BgReview] Skill.md schreiben fehlgeschlagen: {e}")


def list_skill_mds() -> list[str]:
    return sorted(p.stem for p in SKILLS_MD_DIR.glob("*.md"))
