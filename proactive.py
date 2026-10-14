"""
Proaktive Nachrichten – Mantis meldet sich aus eigener Initiative.
Basiert auf LZG-Memories und Tageszeit.
"""
import logging
from datetime import date, datetime

from llm.base import LLMProvider, Message
from memory.lzg import LZG
from memory.chat_messages import load_prompt_chat_messages
import config

try:
    from tools.dashboard import DashboardReader
    _dashboard = DashboardReader()
except Exception:
    _dashboard = None

log = logging.getLogger(__name__)

PROACTIVE_PROMPT = """Du bist Mantis, Timos persönlicher AI-Assistent. Antworte IMMER auf Deutsch.

Du überlegst, ob du Timo jetzt proaktiv etwas schreiben sollst.

Generiere EINE kurze, natürliche Nachricht – wie ein Freund, kein Coach oder Therapeut.

Mögliche Anlässe (Priorität):
1. Sinnvoller Follow-up zu etwas aus dem letzten Gespräch (nur wenn noch offen/relevant)
2. Konkrete Beobachtung aus seinen Daten (Training, Gesundheit, Habits) – neutral, kein Urteil
3. Kurze praktische Info oder Erinnerung die ihm gerade helfen könnte

STRENGE Regeln:
- Max 1-2 Sätze
- Kein "Hallo Timo" – direkt zur Sache
- KEIN Coaching, Verhaltensanalyse oder Moralpredigten
- KEINE Bewertung seines Verhaltens oder Charakters
- KEINE Wiederholung von bereits besprochenen Themen
- Nur Fakten verwenden die Timo tatsächlich mitgeteilt hat
- Im Zweifel: lieber schweigen als eine generische Nachricht senden
- NIEMALS ankündigen etwas zu tun ("ich sende dir gleich...", "ich schicke dir in einer Minute...")
- NIEMALS behaupten etwas getan zu haben was du nicht wirklich getan hast
- NIEMALS Code, Skripte oder Visualisierungen versprechen oder vortäuschen sie zu senden – das kannst du in dieser Nachricht nicht tun

Aktuelle Zeit: {time}
Was du über Timo weißt:
{memories}
{recent_chat}
Generiere jetzt den Gedanken (nur der Text, kein Kommentar):"""

EVALUATE_PROMPT = """Du bist Mantis. Du hast gerade diesen Gedanken generiert:

"{thought}"

Entscheide: Soll ich das JETZT an Timo schicken?

Strenge Kriterien für JA (ALLE müssen erfüllt sein):
- Konkreter, unmittelbarer Mehrwert für Timo
- Neue Information die er noch nicht kennt oder eine offene Aktion
- Kein Coaching, keine Verhaltensanalyse, kein Urteil
- Würde ein guter Freund das auch schicken?

Kriterien für NEIN (eines reicht):
- Generisch oder austauschbar
- Keine neuen Informationen
- Klingt nach Coaching oder Moralappell
- Timo könnte es als nervig oder aufdringlich empfinden
- Kündigt etwas an was nicht in dieser Nachricht passiert ("ich sende dir gleich...", "ich schicke...")
- Behauptet etwas getan zu haben was nicht wirklich passiert ist
- Im Zweifel: NEIN

Antworte NUR mit: JA oder NEIN"""


class ProactiveTracker:
    """Verfolgt wie viele proaktive Nachrichten heute schon gesendet wurden."""

    def __init__(self):
        self._today: date = date.today()
        self._count: int = 0
        # Beim Start so tun als hätten wir gerade gesendet → kein Spam direkt nach Neustart
        self._last_sent: datetime | None = datetime.now()
        # Verhindert Mehrfachzählung von record_ignored() für dieselbe unbeantwortete Nachricht
        self._ignore_recorded_for: datetime | None = None

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._today:
            self._today = today
            self._count = 0
            log.debug("Proaktiv-Zähler für neuen Tag zurückgesetzt")

    def _interval(self) -> int:
        """Mindestabstand – kann durch Selbst-Reflexion angepasst werden."""
        try:
            from core import db
            override = db.get_setting("proactive_interval_override")
            if isinstance(override, (int, float)):
                return int(override)
        except Exception:
            pass
        return config.PROACTIVE_INTERVAL

    def _check_stale_ignore(self) -> None:
        """Prüft ob die letzte proaktive Nachricht unbeantwortet blieb und zählt das
        als Ignore, statt einfach stur weiterzuschreiben. Ohne diesen Check bleibt
        record_ignored() totes Backoff-Logik ohne Wirkung."""
        if not self._last_sent or self._ignore_recorded_for == self._last_sent:
            return
        # Genug Zeit seit dem Senden vergangen, um eine Reaktion zu erwarten?
        if (datetime.now() - self._last_sent).total_seconds() < self._interval():
            return
        try:
            rows = load_prompt_chat_messages(
                role="user", after=self._last_sent, limit=1,
            )
            if not rows:
                self.record_ignored()
            self._ignore_recorded_for = self._last_sent
        except Exception:
            pass

    @staticmethod
    def _is_night(now: datetime) -> bool:
        """Ruhezeit 22:30–06:30 (kein proaktives Schreiben)."""
        night_start = now.replace(hour=22, minute=30, second=0, microsecond=0)
        night_end   = now.replace(hour=6,  minute=30, second=0, microsecond=0)
        return now >= night_start or now < night_end

    def can_send(self) -> bool:
        self._reset_if_new_day()
        self._check_stale_ignore()

        now = datetime.now()
        if self._is_night(now):
            return False

        # Mindestabstand zum letzten proaktiven Message
        if self._last_sent:
            elapsed = (now - self._last_sent).total_seconds()
            if elapsed < self._interval():
                return False

        return True

    def can_send_data_event(self) -> bool:
        """Datenereignis-Bypass: Intervall ignorieren, nur Nacht-Modus beachten."""
        return not self._is_night(datetime.now())

    def record_sent(self) -> None:
        self._reset_if_new_day()
        self._count += 1
        self._last_sent = datetime.now()
        log.info(f"📤 Proaktive Nachricht gesendet ({self._count} heute)")

    def record_user_response(self) -> None:
        """Aufrufen wenn Timo auf eine proaktive Nachricht antwortet → Ignore-Streak brechen
        und ein evtl. verlängertes Intervall zurücksetzen."""
        try:
            from core import db
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('proactive_ignore_streak', '0') "
                "ON CONFLICT (key) DO UPDATE SET value = '0'"
            )
            db.execute(
                "DELETE FROM settings WHERE key = 'proactive_interval_override'"
            )
        except Exception:
            pass

    def record_ignored(self) -> None:
        """Aufrufen wenn eine proaktive Nachricht ohne Reaktion blieb → Decay erhöhen."""
        try:
            from core import db
            row = db.get_setting("proactive_ignore_streak") or 0
            streak = int(row) + 1
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('proactive_ignore_streak', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = %s",
                (str(streak), str(streak)),
            )
            # Bei 3+ Ignores: Interval verlängern (max 2× normal)
            if streak >= 3:
                from config import PROACTIVE_INTERVAL
                new_interval = min(PROACTIVE_INTERVAL * (1 + streak * 0.3), PROACTIVE_INTERVAL * 2)
                db.execute(
                    "INSERT INTO settings (key, value) VALUES ('proactive_interval_override', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = %s",
                    (str(int(new_interval)), str(int(new_interval))),
                )
                log.info(f"📉 Engagement Decay: Intervall auf {int(new_interval)}s erhöht (streak={streak})")
        except Exception:
            pass

    @property
    def count_today(self) -> int:
        self._reset_if_new_day()
        return self._count


class ProactiveEngine:
    """Generiert und sendet proaktive Nachrichten."""

    def __init__(self, llm: LLMProvider, lzg: LZG, claude: LLMProvider | None = None, kg=None):
        self.llm    = llm
        self.lzg    = lzg
        self.claude = claude
        self.kg     = kg  # KnowledgeGraph (optional)

    async def evaluate(self, thought: str) -> bool:
        """
        Lässt Mantis selbst entscheiden ob der Gedanke es wert ist gesendet zu werden.
        Nutzt Claude Code wenn verfügbar (besseres Urteilsvermögen), sonst Qwen.
        """
        prompt = EVALUATE_PROMPT.replace("{thought}", thought)
        try:
            response = await self.llm.chat(
                messages=[Message(role="user", content=prompt)],
                temperature=0.2,
                max_tokens=10,
            )
            verdict = response.strip().upper()
            should_send = verdict.startswith("JA")
            log.debug(f"Bewertung: {verdict} → {'senden' if should_send else 'verwerfen'}")
            return should_send
        except Exception as e:
            log.warning(f"Bewertung fehlgeschlagen: {e}")
            return False

    async def generate(self) -> str | None:
        """Generiert eine proaktive Nachricht basierend auf Memories + Dashboard + Chat-History."""
        try:
            memories = self.lzg.get_all(limit=10)
            memory_ctx = self.lzg.format_for_context(memories)
        except Exception as e:
            log.warning(f"Memory-Abruf für Proaktiv fehlgeschlagen: {e}")
            memory_ctx = "Keine Langzeiterinnerungen verfügbar."

        # Dashboard-Kontext für proaktive Nutzung
        dashboard_ctx = ""
        if _dashboard:
            try:
                health = _dashboard.get_recent_health(days=2)
                events = _dashboard.get_upcoming_events(days=3)
                tasks  = _dashboard.get_open_tasks(limit=5)
                lines  = []
                if health:
                    lines.append("Gesundheit: " + " | ".join(
                        f"{h.date}: {h.steps or 0} Schritte, {h.sleep_duration or 0:.1f}h Schlaf"
                        for h in health[:2]
                    ))
                if events:
                    lines.append("Bevorstehende Termine: " + ", ".join(
                        f"{e.start.strftime('%d.%m. %H:%M') if not e.all_day else e.start.strftime('%d.%m.')} {e.title}"
                        for e in events[:3]
                    ))
                if tasks:
                    lines.append("Offene Tasks: " + ", ".join(t.title for t in tasks[:3]))
                dashboard_ctx = "\n".join(lines)
            except Exception as e:
                log.debug(f"Dashboard-Kontext für Proaktiv fehlgeschlagen: {e}")

        # Letzte Chat-Nachrichten + explizite Blacklist bereits gestellter Fragen
        recent_chat_ctx = ""
        last_proactive_ctx = ""
        try:
            rows = load_prompt_chat_messages(limit=20)
            if rows:
                # Letzte proaktive Mantis-Nachrichten explizit hervorheben
                proactive_msgs = [
                    r["content"][:150]
                    for r in rows
                    if r["role"] == "assistant" and r.get("channel") in ("autopilot", "telegram")
                ][:3]
                if proactive_msgs:
                    last_proactive_ctx = (
                        "\n⛔ BEREITS GESENDETE NACHRICHTEN (diese Themen/Fragen NICHT wiederholen):\n"
                        + "\n".join(f"- {m}" for m in proactive_msgs)
                        + "\n"
                    )

                # Gesamte Chat-History als Kontext
                # Ankündigungs-Phrasen filtern damit das Muster nicht verstärkt wird
                _bad_phrases = ("sende dir", "schicke dir", "ist fertig", "ist bereit",
                                "gleich zu", "in einer minute", "zugestellt", "gesendet",
                                "fertiggestellt", "ist unterwegs")
                rows_conv = list(reversed(rows[:12]))
                lines = []
                for r in rows_conv:
                    role_label = "Timo" if r["role"] == "user" else "Mantis"
                    content = r['content'][:200]
                    # Mantis-Nachrichten die Ankündigungen enthalten überspringen
                    if r["role"] == "assistant" and any(p in content.lower() for p in _bad_phrases):
                        continue
                    lines.append(f"{role_label}: {content}")
                recent_chat_ctx = "\nLetztes Gespräch:\n" + "\n".join(lines) + "\n"
        except Exception as e:
            log.debug(f"Chat-History für Proaktiv fehlgeschlagen: {e}")

        # Wissens-Graph Kontext
        kg_ctx = ""
        if self.kg:
            try:
                kg_ctx = "\n" + self.kg.format_for_context()
            except Exception as e:
                log.debug(f"KG-Kontext für Proaktiv fehlgeschlagen: {e}")

        now = datetime.now().strftime("%A, %d. %B %Y, %H:%M Uhr")
        memories_with_dashboard = memory_ctx + kg_ctx
        if dashboard_ctx:
            memories_with_dashboard += f"\n\nLive-Daten:\n{dashboard_ctx}"
        prompt = PROACTIVE_PROMPT.format(
            time=now,
            memories=memories_with_dashboard,
            recent_chat=recent_chat_ctx + last_proactive_ctx,
        )

        try:
            response = await self.llm.chat(
                messages=[Message(role="user", content=prompt)],
                temperature=0.65,   # Niedrig genug um Generations-Loops zu vermeiden
                max_tokens=150,     # Kurz halten – proaktive Nachrichten müssen prägnant sein
            )
            msg = response.strip()
            if msg:
                log.info(f"💭 Proaktiv generiert: {msg[:60]}...")
            return msg if msg else None
        except Exception as e:
            log.warning(f"Proaktiv-Generierung fehlgeschlagen: {e}")
            return None
