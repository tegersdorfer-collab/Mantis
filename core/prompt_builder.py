"""
PromptBuilder — baut den System-Prompt für jeden Turn zusammen.
Parallel: Memory-Embedding, Dashboard, Tasks, KG, Directives, Warm-Profile, Skill-Prozeduren.
"""
import asyncio
import logging
from datetime import datetime

from core import skill_md, situation
import config

log = logging.getLogger(__name__)

_RECALLLESS_ACKNOWLEDGEMENTS = frozenset({"ja", "nein", "ok", "okay", "mhm", "hm"})


def _is_recallless_acknowledgement(text: str) -> bool:
    """Erkennt knappe Bestätigungen, für die semantischer LZG-Recall nichts ergänzt."""
    normalized = text.strip().casefold().rstrip(".,!?;:… ")
    return normalized in _RECALLLESS_ACKNOWLEDGEMENTS


class CacheableSystemPrompt(str):
    """Systemtext mit Metadaten, wo der stabile, cachebare Präfix endet."""

    def __new__(
        cls, value: str, cache_prefix: str, recall_ids: tuple[int, ...] = (),
    ):
        prompt = super().__new__(cls, value)
        prompt.cache_prefix = cache_prefix
        prompt.recall_ids = tuple(recall_ids)
        return prompt

    def __add__(self, suffix: str):
        if not isinstance(suffix, str):
            return NotImplemented
        return type(self)(
            super().__add__(suffix), cache_prefix=self.cache_prefix,
            recall_ids=self.recall_ids,
        )


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

    def book_recall(self, memory_ids) -> None:
        """Bucht Recall-Zähler und merkt die IDs für die Verifikations-Heuristik."""
        ids = list(dict.fromkeys(memory_ids or ()))
        self._last_mem_ids = ids
        for memory_id in ids:
            try:
                self.forgetting.bump_recall(memory_id)
            except Exception:
                pass

    def memory_context(
        self, query_text: str, embedding, *, speculative: bool = False,
    ) -> tuple[str, tuple[int, ...]]:
        try:
            if embedding is not None:
                mems = self.lzg.search_hybrid(query_text, embedding, top_k=config.LZG_TOP_K)
            else:
                mems = self.lzg.get_all(limit=10)
            memory_ids = tuple(m.id for m in mems)
            if not speculative:
                self.book_recall(memory_ids)
            return self.lzg.format_for_context(mems), memory_ids
        except Exception as e:
            log.debug(f"Memory-Kontext: {e}")
            return "—", ()

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

    async def build(self, user_text: str, *, speculative: bool = False) -> str:
        async def _embed_and_mem():
            if _is_recallless_acknowledgement(user_text) or self.recall_gate(user_text):
                return "—", ()
            try:
                embedding = await self.embed_llm.embed(user_text)
            except Exception as e:
                log.debug(f"Embed fehlgeschlagen: {e}")
                embedding = None
            return await asyncio.to_thread(
                self.memory_context, user_text, embedding, speculative=speculative,
            )

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
        mem_result, dash_ctx, task_ctx, kg_ctx, dir_ctx, warm_profile, skill_ctx = clean
        if isinstance(mem_result, tuple) and len(mem_result) == 2:
            mem_ctx, recall_ids = mem_result
        else:
            mem_ctx, recall_ids = mem_result, ()
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
        cache_prefix = prompt
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
        situation_ctx = situation.prompt_context()
        if situation_ctx:
            prompt += f"\n{situation_ctx}\n"
        return CacheableSystemPrompt(
            prompt, cache_prefix=cache_prefix, recall_ids=recall_ids,
        )
