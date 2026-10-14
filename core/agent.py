"""
Agentischer Kern – ReAct-Loop mit nativem Tool-Calling.
Das Modell entscheidet selbst, welche Tools es in welcher Reihenfolge nutzt.

Backends (core/backends/) übernehmen den LLM-spezifischen Teil:
    OpenRouterBackend → OpenRouter Chat Completions
    ClaudeBackend    → Anthropic Messages
    ChatGPTPlanBackend → ChatGPT Responses

Dieser Loop kennt nur backend.call() — egal welcher Anbieter dahintersteckt.
"""
import json
import logging
import re

from core import tools as toolreg
from core import voice_timing
from core.db import log_event
from core.status import BUS
from core.backends.base import AgentBackend, AttemptEventCb
from core.backends.fallback import FallbackBackend
from core.stream_events import StreamEvent, new_attempt_id

log = logging.getLogger(__name__)

StreamCb = AttemptEventCb


class Agent:
    def __init__(self, backend: AgentBackend, max_steps: int = 8):
        self._backend = backend
        self._max_steps = max_steps

    @property
    def backend(self) -> AgentBackend:
        return self._backend

    @property
    def model_name(self) -> str:
        return self._backend.model_name

    async def _call_backend(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
        temperature: float,
        max_tokens: int,
        think: bool = False,
        force_tool_call: bool = False,
    ) -> tuple[str, list[dict], str | None]:
        timer = voice_timing.LlmCall()
        content, tool_calls, attempt_id = await self._call_backend_inner(
            messages, tools=tools, stream_cb=stream_cb, temperature=temperature,
            max_tokens=max_tokens, think=think, force_tool_call=force_tool_call,
            timer=timer,
        )
        timer.finish(tool_calls)
        return content, tool_calls, attempt_id

    async def _call_backend_inner(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
        temperature: float,
        max_tokens: int,
        think: bool = False,
        force_tool_call: bool = False,
        timer: "voice_timing.LlmCall",
    ) -> tuple[str, list[dict], str | None]:
        if stream_cb is None:
            content, tool_calls = await self._backend.call(
                messages, tools=tools, stream_cb=None,
                temperature=temperature, max_tokens=max_tokens,
                think=think, force_tool_call=force_tool_call,
            )
            return content, tool_calls, None

        if isinstance(self._backend, FallbackBackend):
            active_attempt_id: list[str | None] = [None]

            async def report_event(event: StreamEvent) -> None:
                if event.kind == "delta" and event.text_delta:
                    timer.mark_first_token()
                if event.kind == "attempt_start":
                    active_attempt_id[0] = event.attempt_id
                elif event.kind == "attempt_abort" and active_attempt_id[0] == event.attempt_id:
                    active_attempt_id[0] = None
                await stream_cb(event)

            content, tool_calls = await self._backend.call(
                messages, tools=tools, stream_cb=None,
                temperature=temperature, max_tokens=max_tokens,
                think=think, force_tool_call=force_tool_call,
                attempt_cb=report_event,
            )
            return content, tool_calls, active_attempt_id[0]

        attempt_id = new_attempt_id()
        await stream_cb(StreamEvent("attempt_start", attempt_id))

        async def report_delta(text_delta: str) -> None:
            if text_delta:
                timer.mark_first_token()
            await stream_cb(StreamEvent("delta", attempt_id, text_delta=text_delta))

        try:
            content, tool_calls = await self._backend.call(
                messages, tools=tools, stream_cb=report_delta,
                temperature=temperature, max_tokens=max_tokens,
                think=think, force_tool_call=force_tool_call,
            )
        except BaseException:
            try:
                await stream_cb(StreamEvent("attempt_abort", attempt_id))
            except Exception:
                pass
            raise
        return content, tool_calls, attempt_id

    @staticmethod
    async def _end_attempt(
        stream_cb: StreamCb | None, attempt_id: str | None, *, final: bool,
    ) -> None:
        if stream_cb is not None and attempt_id is not None:
            await stream_cb(StreamEvent("attempt_end", attempt_id, final=final))

    async def run(
        self,
        messages: list[dict],
        system: str,
        stream_cb: StreamCb | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1500,
        use_tools: bool = True,
        allowed_tools: list[str] | None = None,
        force_tools: bool = False,
        think: bool = False,
        dry_run_tools: bool = False,
    ) -> tuple[str, list[dict]]:
        """
        Führt den ReAct-Loop aus.
        Gibt (finale_antwort, tool_trace) zurück.
        tool_trace = [{"tool": name, "args": {...}, "result": "..."}]

        dry_run_tools: Tool-Calls werden aufgezeichnet aber NICHT ausgeführt
        (für die Eval-Suite — kein Test-Müll in der Produktions-DB).
        """
        norm: list[dict] = []
        for m in messages:
            if isinstance(m, dict):
                norm.append(m)
            else:
                norm.append({"role": m.role, "content": m.content})

        msgs: list[dict] = [{"role": "system", "content": system}] + norm

        if not use_tools or allowed_tools == []:
            schemas = None
        elif allowed_tools:
            schemas = toolreg.ollama_schemas(only=allowed_tools)
        else:
            schemas = toolreg.ollama_schemas()

        # Schemas guide the model; enforce the same permissions at dispatch too.
        permitted_tools = {s["function"]["name"] for s in (schemas or [])}
        trace: list[dict] = []

        for step in range(self._max_steps):
            is_first = step == 0
            force = force_tools and is_first and bool(schemas)
            BUS.emit("thinking", "Denke nach…", detail=self._backend.model_name)
            content, tool_calls, attempt_id = await self._call_backend(
                msgs,
                tools=schemas,
                stream_cb=stream_cb,
                temperature=temperature,
                max_tokens=max_tokens,
                think=think and is_first,
                force_tool_call=force,
            )

            if not tool_calls:
                # Math-Guard: Antwort enthält Rechnung ohne calculate aufgerufen?
                if "calculate" in permitted_tools and _needs_calculate(content) and not any(
                    t["tool"] == "calculate" for t in trace
                ):
                    log.debug("Math-Guard: erzwinge calculate-Tool-Call")
                    await self._end_attempt(stream_cb, attempt_id, final=False)
                    msgs.append({"role": "assistant", "content": content or ""})
                    msgs.append({
                        "role": "user",
                        "content": "[SYSTEM] Du hast gerade eine Zahl berechnet ohne das calculate-Tool zu nutzen. "
                                   "Das ist verboten. Rufe jetzt calculate mit dem korrekten Ausdruck auf "
                                   "und gib danach die korrigierte Antwort.",
                    })
                    content, tool_calls, attempt_id = await self._call_backend(
                        msgs, tools=schemas, stream_cb=stream_cb,
                        temperature=0.2, max_tokens=300,
                    )
                    msgs = msgs[:-2]
                    if not tool_calls:
                        await self._end_attempt(stream_cb, attempt_id, final=True)
                        return content.strip(), trace
                    await self._end_attempt(stream_cb, attempt_id, final=False)
                    # tool_calls erhalten → unten direkt ausführen (kein continue:
                    # das würde sie verwerfen und einen zweiten LLM-Call machen)

                # Kein Tool-Call obwohl erwartet → einmal retry
                elif force_tools and is_first and schemas:
                    log.debug("Kein Tool-Call trotz force_tools – retry")
                    await self._end_attempt(stream_cb, attempt_id, final=False)
                    msgs.append({"role": "assistant", "content": content or ""})
                    msgs.append({
                        "role": "user",
                        "content": "[SYSTEM] Du hast gerade KEIN Tool aufgerufen. "
                                   "Rufe jetzt sofort das passende Tool auf – antworte nicht mit Text.",
                    })
                    content, tool_calls, attempt_id = await self._call_backend(
                        msgs, tools=schemas, stream_cb=stream_cb,
                        temperature=0.3, max_tokens=200,
                    )
                    if not tool_calls:
                        await self._end_attempt(stream_cb, attempt_id, final=True)
                        return content.strip() or (msgs[-2].get("content", "")).strip(), trace
                    await self._end_attempt(stream_cb, attempt_id, final=False)
                    msgs = msgs[:-2]
                else:
                    await self._end_attempt(stream_cb, attempt_id, final=True)
                    return content.strip(), trace
            else:
                await self._end_attempt(stream_cb, attempt_id, final=False)

            # Tool-Calls ausführen
            msgs.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                BUS.emit("tool", f"🔧 {name}", detail=_short(args) or None)
                if name not in permitted_tools:
                    result = f"FEHLER: Tool '{name}' ist in diesem Lauf nicht erlaubt."
                elif dry_run_tools:
                    result = f"[Eval-Dry-Run] Tool '{name}' würde ausgeführt (Argumente akzeptiert)."
                else:
                    with voice_timing.phase("tool_exec_ms"):
                        result = await toolreg.execute(name, args)
                trace.append({"tool": name, "args": args, "result": result[:500]})
                if not dry_run_tools:
                    if name.startswith("inbox_"):
                        status = "error" if result.startswith(("FEHLER", "⚠️")) else "completed"
                        log_event("tool", name, {"status": status})
                    else:
                        log_event("tool", f"{name}({_short(args)})", {"result": result[:300]})
                msgs.append({
                    "role": "tool",
                    "content": result,
                    "name": name,
                    "tool_use_id": tc.get("tool_use_id") or tc.get("id", ""),
                })

        # Max-Steps erreicht
        content, _tool_calls, attempt_id = await self._call_backend(
            msgs, tools=None, stream_cb=stream_cb,
            temperature=temperature, max_tokens=max_tokens,
        )
        await self._end_attempt(stream_cb, attempt_id, final=True)
        return content.strip(), trace

    async def warmup(self) -> None:
        await self._backend.warmup()


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

_MATH_PATTERNS = re.compile(
    r"(~?\d+[\.,]\d+\s*/\s*\w+|"
    r"Mittelwert\s*[~≈]?\s*\d+|"
    r"Durchschnitt\s*[~≈]?\s*\d+|"
    r"\d+\s*[÷/]\s*\d+\s*=|"
    r"[~≈]\s*\d+\s*(pro|per|/)\s*\w+)",
    re.IGNORECASE,
)


def _needs_calculate(text: str) -> bool:
    return bool(_MATH_PATTERNS.search(text or ""))


def _short(args: dict) -> str:
    try:
        return json.dumps(args, ensure_ascii=False)[:60]
    except Exception:
        return str(args)[:60]
