"""
OpenRouter-Backend für den Agent-Loop (OpenAI-kompatible Chat-Completions).

Gedacht als günstiges Cloud-Backend für Tests, bei denen der Mac nicht
zusätzlich ein lokales Modell warmhalten soll — z.B. der iPhone-Field-Client,
der Voice, Routing und Mantis gleichzeitig braucht. Jev bleibt davon
unberührt (core/decide.py); hier geht es nur um die Sprachantwort des Agenten.

Übersetzt das einheitliche Backend-Interface (Ollama-Format für Messages und
Tool-Calls) verlustfrei ins OpenAI-Format und zurück. Kein SDK — httpx reicht.
"""
import json
import logging
import uuid

import httpx

from core.backends.base import AgentBackend, StreamCb
import config

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_S = config.CHAT_CLOUD_TIMEOUT_S


def _args_to_json(args) -> str:
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _msgs_to_openai(messages: list[dict]) -> list[dict]:
    """Ollama-Messages → OpenAI-Messages.

    Unterschiede: Tool-Call-Argumente müssen ein JSON-String sein, Tool-Results
    referenzieren den Call über `tool_call_id`, und jeder Tool-Call braucht eine
    ID. Fehlt sie (Ollama vergibt keine), wird eine erzeugt und für das
    folgende Tool-Result in Aufrufreihenfolge wiederverwendet.
    """
    result: list[dict] = []
    pending_ids: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            entry: dict = {"role": "assistant", "content": m.get("content") or ""}
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                call_id = tc.get("id") or tc.get("tool_use_id") or f"call_{uuid.uuid4().hex[:12]}"
                pending_ids.append(call_id)
                calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": _args_to_json(fn.get("arguments"))},
                })
            if calls:
                entry["tool_calls"] = calls
                # OpenAI-kompatible Endpunkte lehnen `content: ""` neben tool_calls teils ab.
                if not entry["content"]:
                    entry["content"] = None
            result.append(entry)
        elif role == "tool":
            call_id = m.get("tool_use_id") or m.get("tool_call_id")
            if not call_id:
                call_id = pending_ids.pop(0) if pending_ids else f"call_{uuid.uuid4().hex[:12]}"
            elif call_id in pending_ids:
                pending_ids.remove(call_id)
            result.append({"role": "tool", "content": str(m.get("content", "")), "tool_call_id": call_id})
        else:
            result.append({"role": role, "content": m.get("content", "")})
    return result


def _extract_tool_calls(message: dict, tools: list[dict] | None = None) -> list[dict]:
    """OpenAI-Tool-Calls → einheitliches Backend-Format (Argumente als dict)."""
    required_by_name = {}
    for tool in tools or []:
        fn_schema = tool.get("function") or {}
        params = fn_schema.get("parameters") or {}
        required_by_name[fn_schema.get("name", "")] = params.get("required") or []

    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        if "arguments" not in fn:
            required = required_by_name.get(fn.get("name", ""), [])
            if required:
                raise ValueError(
                    f"OpenRouter Tool-Argumente fehlen für Pflichtargumente: {', '.join(required)}"
                )
            args = {}
        else:
            args = fn["arguments"]
        if isinstance(args, str):
            if not args.strip():
                raise ValueError("OpenRouter Tool-Argumente enthalten ungültiges JSON")
            try:
                args = json.loads(args)
            except (TypeError, ValueError) as exc:
                raise ValueError("OpenRouter Tool-Argumente enthalten ungültiges JSON") from exc
        if not isinstance(args, dict):
            raise ValueError("OpenRouter Tool-Argumente müssen ein JSON-Objekt sein")
        calls.append({"id": tc.get("id", ""), "function": {"name": fn.get("name", ""), "arguments": args}})
    return calls


class OpenRouterBackend(AgentBackend):
    def __init__(self, model: str, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None,
                 reasoning_effort: str | None = None,
                 purpose: str = "chat-agent"):
        self._model = model
        # Reasoning-Modelle (GLM 5.3, DeepSeek V4.x) denken bei OpenRouter per
        # Default unbegrenzt und lassen sich teils nicht abschalten ("Reasoning
        # is mandatory") — Feldtest 22.09.2026: 1500 Tokens Denken, 53 s, leere
        # Antwort. effort=low drosselt auf ~1 Token bei korrektem Tool-Call.
        # Leerer String = Feld nicht senden.
        self._reasoning_effort = (reasoning_effort if reasoning_effort is not None
                                  else config.CHAT_CLOUD_REASONING_EFFORT)
        self._purpose = purpose
        raw_key = api_key if api_key is not None else config.OPENROUTER_API_KEY
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("OpenRouterBackend braucht OPENROUTER_API_KEY")
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("OpenRouter API-Key ist ungültig")
        self._api_key = key
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_S)
        self._headers = {
            "Authorization": f"Bearer {key}",
            # OpenRouter-Konvention zur App-Zuordnung; rein informativ.
            "HTTP-Referer": "https://github.com/tegersdorfer-collab/mantis",
            "X-Title": "Mantis",
        }

    @property
    def model_name(self) -> str:
        return self._model

    async def warmup(self) -> None:
        return None  # Cloud — nichts vorzuladen.

    def _payload(self, messages, tools, temperature, max_tokens, force_tool_call) -> dict:
        payload: dict = {
            "model": self._model,
            "messages": _msgs_to_openai(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
            if force_tool_call:
                payload["tool_choice"] = "required"
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}
        return payload

    async def call(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
        temperature: float,
        max_tokens: int,
        think: bool = False,
        force_tool_call: bool = False,
    ) -> tuple[str, list[dict]]:
        payload = self._payload(messages, tools, temperature, max_tokens, force_tool_call)
        if stream_cb:
            return await self._call_streaming(payload, stream_cb)

        resp = await self._client.post("/chat/completions", json=payload, headers=self._headers)
        resp.raise_for_status()
        data = resp.json()
        self._record_usage(data.get("usage"), data.get("id"))
        message = (data.get("choices") or [{}])[0].get("message") or {}
        return message.get("content") or "", _extract_tool_calls(message, tools)

    async def _call_streaming(self, payload: dict, stream_cb: StreamCb) -> tuple[str, list[dict]]:
        payload = {**payload, "stream": True}
        parts: list[str] = []
        # Tool-Call-Fragmente kommen pro `index` verteilt über mehrere Deltas.
        calls_by_index: dict[int, dict] = {}
        usage = None
        generation_id = None
        last_len = 0
        completed = False
        finish_reason = None

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload,
                                           headers=self._headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # SSE-Kommentare (": OPENROUTER PROCESSING") u.ä.
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    if raw == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError as exc:
                        raise RuntimeError("OpenRouter-Stream enthält ungültiges JSON") from exc
                    if not isinstance(chunk, dict):
                        raise RuntimeError("OpenRouter-Stream enthält ein ungültiges Ereignis")
                    generation_id = chunk.get("id") or generation_id
                    if chunk.get("usage") is not None:
                        if not isinstance(chunk["usage"], dict):
                            raise RuntimeError("OpenRouter-Stream enthält ungültige Usage-Daten")
                        usage = chunk["usage"]
                    if "error" in chunk:
                        raise RuntimeError("OpenRouter-Stream meldet einen API-Fehler")

                    # Nach dem Abschluss können weitere Events die Usage-Trailer
                    # enthalten. Deren Choices beeinflussen die Antwort nicht mehr.
                    if finish_reason is not None:
                        continue

                    choices = chunk.get("choices") or []
                    if not isinstance(choices, list):
                        raise RuntimeError("OpenRouter-Stream enthält ungültige Choices")
                    choice = choices[0] if choices else {}
                    if not isinstance(choice, dict):
                        raise RuntimeError("OpenRouter-Stream enthält eine ungültige Choice")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise RuntimeError("OpenRouter-Stream enthält ein ungültiges Delta")
                    if delta.get("content"):
                        parts.append(delta["content"])
                        full = "".join(parts)
                        if len(full) - last_len >= 50:
                            last_len = len(full)
                            try:
                                await stream_cb(full)
                            except Exception:
                                pass
                    for frag in delta.get("tool_calls") or []:
                        idx = frag.get("index", 0)
                        slot = calls_by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice["finish_reason"]
                        completed = True

            if not completed:
                raise RuntimeError("OpenRouter-Stream endete ohne Abschluss")
            if finish_reason is not None and finish_reason not in {"stop", "tool_calls", "length"}:
                raise RuntimeError(f"OpenRouter-Stream mit finish_reason {finish_reason!r} beendet")
            full = "".join(parts)
            message = {"tool_calls": []}
            for _, slot in sorted(calls_by_index.items()):
                function = {"name": slot["name"]}
                if slot["arguments"]:
                    function["arguments"] = slot["arguments"]
                message["tool_calls"].append({"id": slot["id"], "function": function})
            tool_calls = _extract_tool_calls(message, payload.get("tools"))
        except BaseException:
            # wait_for() cancels the stream on timeout; preserve any usage that
            # arrived before cancellation so the generation can still be billed.
            self._record_usage(usage, generation_id)
            raise

        self._record_usage(usage, generation_id)
        if full and not tool_calls:
            try:
                await stream_cb(full)
            except Exception:
                pass
        return full, tool_calls

    def _record_usage(self, usage: dict | None, generation_id: str | None = None) -> None:
        try:
            from core import llm_usage
            if not usage and not generation_id:
                return
            actual_cost = None
            if usage and usage.get("cost") is not None:
                try:
                    actual_cost = float(usage["cost"])
                except (TypeError, ValueError):
                    actual_cost = None
            llm_usage.record(
                "openrouter", self._model,
                int((usage or {}).get("prompt_tokens") or 0),
                int((usage or {}).get("completion_tokens") or 0),
                purpose=self._purpose,
                actual_cost=actual_cost,
                generation_id=generation_id,
                api_key=self._api_key,
                generation_client=self._client,
            )
        except Exception:
            pass
