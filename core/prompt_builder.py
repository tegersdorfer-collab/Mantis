"""
PromptBuilder — baut den System-Prompt für jeden Turn zusammen.
Parallel: Memory-Embedding, Dashboard, Tasks, KG, Directives, Warm-Profile, Skill-Prozeduren.
"""
import asyncio
import logging
from datetime import datetime

from core import skill_md
import config

log = logging.getLogger(__name__)


class PromptBuilder:
    def __init__(self, embed_llm, lzg, kzg, kg, reflection, dashboard, forgetting, identity: str):
        self.embed_llm  = embed_llm
        self.lzg        = lzg
        self.kzg        = kzg
        self.kg         = kg
        self.reflection = reflection
        self.dashboard  = dashboard
        self.forgetting = forgetting
        self.identity   = identity
        self._last_mem_ids: list[int] = []

    # ── Recall Gate ───────────────────────────────────────────────────────────

    def recall_gate(self, query: str) -> bool:
        """Jaccard-Heuristik: überspringt pgvector wenn KZG bereits ≥50% abdeckt."""
        try:
            turns = self.kzg.get_recent_turns(n=6)
            if len(turns) < 3:
                return False
            kzg_words = {w.lower() for t in turns for w in t.content.split() if len(w) > 3}
            query_words = {w.lower() for w in query.split() if len(w) > 3}
            if not query_words:
                return False
            overlap = len(query_words & kzg_words) / len(query_words)
            if overlap >= 0.5:
                log.debug(f"Recall Gate: übersprungen (Jaccard={overlap:.2f})")
                return True
        except Exception:
            pass
        return False

    # ── Memory / KG Context ───────────────────────────────────────────────────

    def memory_context(self, query_text: str, embedding) -> str:
        try:
            if embedding is not None:
                mems = self.lzg.search_hybrid(query_text, embedding, top_k=config.LZG_TOP_K)
            else:
                mems = self.lzg.get_all(limit=10)
            self._last_mem_ids = [m.id for m in mems]
            for m in mems:
                try:
                    self.forgetting.bump_recall(m.id)
                except Exception:
                    pass
            return self.lzg.format_for_context(mems)
        except Exception as e:
            log.debug(f"Memory-Kontext: {e}")
            return "—"

    def kg_context(self) -> str:
        try:
            return self.kg.format_for_context()
        except Exception as e:
            log.debug(f"KG-Kontext: {e}")
            return ""

    def _task_ctx(self) -> str:
        try:
            from domains import tasks as tasks_d
            return tasks_d.context_summary(8)
        except Exception:
            return ""

    def _dashboard_ctx(self) -> str:
        try:
            return self.dashboard.format_for_context()
        except Exception:
            return ""

    # ── System-Prompt zusammenbauen ───────────────────────────────────────────

    async def build(self, user_text: str) -> str:
        async def _embed_and_mem():
            if self.recall_gate(user_text):
                return "—"
            try:
                embedding = await self.embed_llm.embed(user_text)
            except Exception as e:
                log.debug(f"Embed fehlgeschlagen: {e}")
                embedding = None
            return await asyncio.to_thread(self.memory_context, user_text, embedding)

        results = await asyncio.gather(
            _embed_and_mem(),
            asyncio.to_thread(self._dashboard_ctx),
            asyncio.to_thread(self._task_ctx),
            asyncio.to_thread(self.kg_context),
            asyncio.to_thread(self.kg.format_directives),
            asyncio.to_thread(self.kg.warm_profile),
            asyncio.to_thread(lambda: skill_md.build_skill_context(user_text)),
            return_exceptions=True,
        )
        # Ein kaputter Kontext-Baustein darf niemals den ganzen Turn killen —
        # degradierter Prompt statt toter Chat.
        labels = ("memory", "dashboard", "tasks", "kg", "directives", "warm_profile", "skills")
        clean = []
        for label, r in zip(labels, results):
            if isinstance(r, BaseException):
                log.warning(f"Prompt-Kontext '{label}' fehlgeschlagen: {r}")
                clean.append("")
            else:
                clean.append(r)
        (mem_ctx, dash_ctx, task_ctx, kg_ctx, dir_ctx, warm_profile, skill_ctx) = clean
        mem_ctx = mem_ctx or "—"

        behavior = self.reflection.behavior_notes()
        now = datetime.now()
        WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        now_str = f"{WD[now.weekday()]}, {now.strftime('%d.%m.%Y, %H:%M')} Uhr"

        prompt = self.identity
        prompt += (
            "\n\n## Werkzeuge\n"
            "Du hast Zugriff auf viele Tools (Tasks, Reminder, Kalender, Habits, Fitness, "
            "Ernährung, Journal, Ziele, Health-Daten, Wetter, Web-Suche, Gedächtnis). "
            "Nutze sie EIGENSTÄNDIG wenn Timo etwas erledigt oder wissen will – frag nicht um Erlaubnis, "
            "handle. Antworte danach kurz und natürlich auf Deutsch.\n"
            "KRITISCH: Wenn Timo eine AKTION verlangt (löschen, erstellen, eintragen, verschieben, "
            "erinnern, abhaken etc.) MUSST du das passende Tool aufrufen. NIEMALS sagen 'ich habe "
            "keine Funktion dafür' oder 'ich kann das nicht' wenn ein passendes Tool existiert. "
            "Bestätige eine Aktion NIE, ohne das Tool wirklich aufgerufen zu haben.\n"
            "BEISPIELE: 'leg eine Aufgabe an: Steuer' → create_task aufrufen. "
            "'wie war mein Schlaf' → get_health aufrufen. 'erinnere mich um 9 an X' → set_reminder. "
            "Erst das Tool, dann eine kurze Bestätigung.\n"
            "AUFGABEN-ZUWEISUNG: Wenn du create_task aufrufst, entscheidet das System automatisch "
            "ob du (Mantis) oder Timo die Aufgabe bekommt – ruf einfach create_task auf.\n"
            "FÄHIGKEITSLÜCKEN: Wenn KEIN passendes Tool existiert, kann create_skill einen "
            "inaktiven Python-Entwurf speichern. Manuelle Prüfung und Integration sind nötig; "
            "der Entwurf erweitert deine verfügbaren Tools nicht automatisch.\n"
            "ZEIT/DATUM: Nutze den Wert aus '## Aktuell'. Für Termine: 'morgen 14:00', 'heute 18:30' "
            "oder 'TT.MM.JJJJ HH:MM' – immer LOKALE Zeit, niemals UTC.\n"
        )
        if warm_profile:
            prompt += f"\n{warm_profile}\n"
        if dir_ctx:
            prompt += f"\n{dir_ctx}\n"
        if skill_ctx:
            prompt += f"\n{skill_ctx}\n"
        if behavior:
            prompt += f"\n{behavior}\n"
        prompt += f"\n## Was du über Timo weißt:\n{mem_ctx}\n"
        if kg_ctx:
            prompt += f"\n{kg_ctx}\n"
        if task_ctx:
            prompt += f"\n## Offene Aufgaben:\n{task_ctx}\n"
        if dash_ctx:
            prompt += f"\n## Live-Daten (Dashboard):\n{dash_ctx}\n"
        prompt += f"\n## Aktuell:\n{now_str}\n"
        return prompt
