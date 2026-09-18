"""
MessageHandler — verarbeitet eingehende Nachrichten aus Telegram und Dashboard.
Zuständig für: ReAct-Loop, Streaming, Post-Turn-Learning, Persistenz.
"""
import asyncio
import json
import logging
import time

from communication.base import IncomingMessage
from core.background_review import run_background_review
from core.status import BUS
from core import skills, db, ui_state, fast_commands, skill_request

log = logging.getLogger(__name__)

_CONFIRM_WORDS = {
    "ja", "jo", "jap", "jop", "genau", "exakt", "stimmt", "richtig", "korrekt",
    "ja genau", "ja stimmt", "ja richtig", "ja korrekt", "ja exakt",
    "yep", "yes", "correct", "right", "exactly", "true", "confirmed",
}


def _safe_task(coro, label: str):
    async def _wrapper():
        try:
            await coro
        except Exception as e:
            log.warning(f"Background-Task '{label}' fehlgeschlagen: {e}", exc_info=True)
            db.log_error(label, e)
    return asyncio.create_task(_wrapper())


class MessageHandler:
    def __init__(self, kzg, lzg, agent, prompt_builder, channel,
                 proactive_tracker, forgetting, extractor, bg_llm,
                 alphaprogression, on_user_active):
        self.kzg               = kzg
        self.lzg               = lzg
        self.agent             = agent
        self.prompt_builder    = prompt_builder
        self.channel           = channel
        self.proactive_tracker = proactive_tracker
        self.forgetting        = forgetting
        self.extractor         = extractor
        self.bg_llm            = bg_llm
        self.alphaprogression  = alphaprogression
        self.on_user_active    = on_user_active   # callback: () → None

    # ── Verification Bump ─────────────────────────────────────────────────────

    def _check_verification_bump(self, text: str) -> None:
        lower = text.strip().lower()
        if any(lower == w or lower.startswith(w + " ") or lower.startswith(w + ",")
               for w in _CONFIRM_WORDS):
            for mid in getattr(self.prompt_builder, "_last_mem_ids", []):
                try:
                    self.forgetting.bump_recall(mid)
                except Exception:
                    pass

    # ── Alpha Progression Link ────────────────────────────────────────────────

    async def _handle_alpha_progression(self, text: str, channel_name: str) -> str | None:
        url = self.alphaprogression.extract_link(text)
        if not url:
            return None
        BUS.emit("thinking", "Alpha Progression wird importiert…")
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, self.alphaprogression.fetch_and_log, url)
        except Exception as e:
            log.error(f"AP import error: {e}")
            response = f"❌ Fehler beim Importieren: {e}"
        self.kzg.add("assistant", response)
        self._persist_msg("assistant", response, channel=channel_name)
        BUS.emit("response", response)
        return response

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def handle(self, msg: IncomingMessage, resume_idle_cb) -> None:
        t0 = time.time()
        BUS.emit("thinking", "Nachricht empfangen…", detail=msg.text[:60])
        log.info(f"Eingehend: {msg.text[:60]}")

        self.kzg.add("user", msg.text)
        self._persist_msg("user", msg.text, channel="telegram")
        self._check_verification_bump(msg.text)
        self.on_user_active()

        # Engagement Decay
        try:
            last_turns = self.kzg.get_recent_turns(n=2)
            if last_turns and last_turns[-1].role == "assistant":
                self.proactive_tracker.record_user_response()
        except Exception:
            pass

        # Task-Klärungsantwort
        waiting = db.query(
            "SELECT id FROM tasks WHERE execution_phase='waiting_clarification' "
            "AND clarification_answer IS NULL ORDER BY created_at DESC LIMIT 1"
        )
        if waiting:
            db.execute(
                "UPDATE tasks SET clarification_answer=%s, execution_phase='executing', "
                "hold_until=NULL WHERE id=%s",
                (msg.text, waiting[0]["id"])
            )
            response = "Danke! Ich mache mit deiner Antwort weiter an der Aufgabe. 👍"
            self.kzg.add("assistant", response)
            self._persist_msg("assistant", response)
            BUS.emit("response", response)
            resume_idle_cb()
            return

        # Accountability & Momentum: eingehende Antwort auf einen offenen
        # Anchor/Block-Prompt verbuchen (deterministisch, kein LLM). Bewusst VOR
        # dem Agenten — sonst würde das Chat-Modell die sekundenkurze Interaktion
        # in ein Gespräch verwandeln (genau das will die Spec nicht).
        from domains import accountability
        acc_reply = accountability.intercept(msg.text)
        if acc_reply is not None:
            self.kzg.add("assistant", acc_reply)
            self._persist_msg("assistant", acc_reply)
            BUS.emit("response", acc_reply)
            await self.channel.send(acc_reply)
            resume_idle_cb()
            return

        # Alpha Progression
        if await self._handle_alpha_progression(msg.text, "telegram"):
            resume_idle_cb()
            return

        # Agent-Loop
        stream_cb, finalize = await self._make_stream()
        response, trace = await self._agent_turn(msg.text, channel_name="telegram",
                                                 stream_cb=stream_cb)
        if not response:
            response = "…"
        await finalize(response)
        self._finish_turn(msg.text, response, trace, channel_name="telegram")

        elapsed = time.time() - t0
        log.info(f"Antwort in {elapsed:.1f}s ({len(trace)} Tools, {self.agent.model_name})")
        BUS.emit("idle", "Bereit", detail=f"{elapsed:.1f}s · {self.agent.model_name}")
        resume_idle_cb()

    # ── Dashboard ─────────────────────────────────────────────────────────────

    async def dashboard_respond(self, text: str, stream_cb=None, agent=None) -> tuple[str, list]:
        self.on_user_active()
        self.kzg.add("user", text)
        self._persist_msg("user", text, channel="dashboard")

        if await self._handle_alpha_progression(text, "dashboard"):
            return "", []

        response, trace = await self._agent_turn(text, channel_name="dashboard",
                                                 stream_cb=stream_cb, agent=agent)
        self._finish_turn(text, response, trace, channel_name="dashboard", agent=agent)
        return response, trace

    # ── Gemeinsamer Agent-Turn (Telegram + Dashboard) ─────────────────────────

    async def _agent_turn(self, text: str, channel_name: str,
                          stream_cb=None, agent=None) -> tuple[str, list]:
        """Prompt bauen, Tools wählen, Agent laufen lassen — mit Fehler-Fallback."""
        agent = agent or self.agent

        # Deterministischer Fast-Path für kritische Fixbefehle (Licht, Not-Stopp):
        # ruft das Tool DIREKT, ohne LLM — 100% zuverlässig, sofort, modellunabhängig.
        # (Kleine Modelle täuschen sonst unter dem großen Prompt Erfolg vor.)
        fast = fast_commands.match(text)
        if fast is not None:
            try:
                result = await skills.T.execute(fast.tool, fast.args)
            except Exception as e:
                result = f"❌ {fast.tool} fehlgeschlagen: {e}"
            log.info("⚡ Fast-Path %s → %s(%s)", fast.label, fast.tool, fast.args)
            return result, [{"tool": fast.tool, "args": fast.args, "result": result[:500]}]

        # Skill-Factory: eigener Fast-Path, weil hier Quellcode erzeugt werden muss
        # und der Agent das Pflichtargument skill_name reproduzierbar verschluckt
        # (siehe core/skill_request.py). Name und Beschreibung kommen determi-
        # nistisch aus der Äußerung, nur der Code aus einem engen LLM-Call.
        skill_req = skill_request.match(text)
        if skill_req is not None:
            try:
                result = await skill_request.erzeuge(skill_req, self.bg_llm)
            except Exception as e:
                log.error(f"Skill-Fast-Path fehlgeschlagen: {e}")
                db.log_error("skill_fast_path", e)
                result = f"❌ Skill-Erstellung fehlgeschlagen: {e}"
            args = {"skill_name": skill_req.skill_name, "description": skill_req.spec}
            return result, [{"tool": "create_skill", "args": args, "result": result[:500]}]

        # Parallel statt sequenziell: der Jev-Roundtrip in select_tools_async läuft
        # während der Prompt gebaut wird, statt hinterherzuhängen.
        system, (allowed, force_tools) = await asyncio.gather(
            self.prompt_builder.build(text),
            skills.T.select_tools_async(text),
        )
        try:
            return await agent.run(
                messages=self.kzg.recent_messages(max_tokens=3000),
                system=system, stream_cb=stream_cb,
                allowed_tools=allowed, force_tools=force_tools,
                temperature=0.75, max_tokens=1500,
            )
        except Exception as e:
            log.error(f"Agent-Fehler ({channel_name}): {e}")
            db.log_error(f"agent_turn/{channel_name}", e)
            return "⚠️ Technisches Problem, versuch es nochmal.", []

    def _finish_turn(self, user_text: str, response: str, trace: list,
                     channel_name: str, agent=None) -> None:
        """Antwort in KZG/DB persistieren, UI aktualisieren, Post-Turn-Learning starten."""
        agent = agent or self.agent
        self.kzg.add("assistant", response)
        tools_used = [t["tool"] for t in trace]
        _safe_task(ui_state.maybe_update_ui(tools_used), "ui_update")
        self._persist_msg("assistant", response, channel=channel_name,
                          meta={"tools": tools_used, "model": agent.model_name})
        _safe_task(self._post_turn(user_text, response, tools_used), "post_turn")

    # ── Post-Turn Learning ────────────────────────────────────────────────────

    async def _post_turn(self, user_text: str, response: str,
                         tools_used: list[str] | None = None) -> None:
        BUS.emit("learning", "Analysiere Gespräch…")
        n = await self.extractor.extract_from_exchange(user_text, response)
        if n > 0:
            BUS.emit("memory", f"🧠 {n} neue{'r' if n==1 else ''} Fakt{'' if n==1 else 'en'} gespeichert")
        else:
            BUS.emit("idle", "Bereit")

        _safe_task(
            run_background_review(
                user_text=user_text,
                assistant_response=response,
                tools_used=tools_used or [],
                bg_llm=self.bg_llm,
                lzg=self.lzg,
            ),
            "background_review"
        )

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def _make_stream(self):
        if not getattr(self.channel, "supports_streaming", False):
            await self.channel.send_typing()
            async def finalize_plain(text):
                await self.channel.send(text)
            return None, finalize_plain

        msg_id = await self.channel.start_message("💭 …")
        last_edit = [0.0]

        async def stream_cb(full: str):
            now = time.time()
            if now - last_edit[0] < 0.8:
                return
            last_edit[0] = now
            await self.channel.edit_message(msg_id, full)

        async def finalize(text: str):
            await self.channel.edit_message(msg_id, text, markdown=True)

        return stream_cb, finalize

    # ── Persistenz ────────────────────────────────────────────────────────────

    def _persist_msg(self, role: str, content: str, channel: str = "telegram",
                     meta: dict | None = None) -> None:
        try:
            self.lzg.save_kzg_turn(role, content)
        except Exception as e:
            log.warning(f"KZG-Turn nicht gespeichert: {e}")
        try:
            db.execute(
                "INSERT INTO chat_messages (role, content, channel, meta) VALUES (%s,%s,%s,%s)",
                (role, content, channel, json.dumps(meta or {})),
            )
        except Exception as e:
            log.warning(f"Chat-Nachricht nicht persistiert: {e}")
