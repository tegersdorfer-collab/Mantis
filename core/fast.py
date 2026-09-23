"""
Schneller Hilfs-LLM für günstige Entscheidungen:
Klassifikation, Ja/Nein-Urteile, kurze Zusammenfassungen.
Nutzt AGENT_MODEL_FAST (standardmäßig qwen3.5:9b via .env) – gleiches Modell wie Hauptgespräche, kein extra RAM.
"""
import logging

import ollama as _ollama

from core.llm_gate import GATE
import config

log = logging.getLogger(__name__)
_client = _ollama.AsyncClient(host=config.OLLAMA_BASE_URL)


async def ask(prompt: str, max_tokens: int = 20, temperature: float = 0.1, model: str | None = None) -> str:
    try:
        active_model = model or config.AGENT_MODEL_FAST
        async with GATE:
            resp = await _client.chat(
                model=active_model,
                messages=[{"role": "user", "content": prompt}],
                options=config.ollama_options(
                    active_model, temperature=temperature, num_predict=max_tokens
                ),
                keep_alive=config.OLLAMA_KEEP_ALIVE,
                think=False,  # Reasoning-Modelle (z.B. qwen3.5) verbrauchen sonst das
                              # ganze num_predict-Budget für <think>, message.content bleibt leer
            )
        return (resp.message.content or "").strip()
    except Exception as e:
        log.debug(f"fast.ask fehlgeschlagen: {e}")
        return ""


async def yes_no(question: str, model: str | None = None) -> bool:
    out = await ask(f"{question}\n\nAntworte NUR mit JA oder NEIN.", max_tokens=5, model=model)
    return out.strip().upper().startswith("JA") or out.strip().upper().startswith("YES")


async def warmup() -> None:
    try:
        await _client.chat(
            model=config.AGENT_MODEL_FAST,
            messages=[{"role": "user", "content": "ok"}],
            options=config.ollama_options(config.AGENT_MODEL_FAST, num_predict=1),
            keep_alive=config.OLLAMA_KEEP_ALIVE,
        )
        log.info(f"🔥 Schnellmodell {config.AGENT_MODEL_FAST} aufgewärmt")
    except Exception as e:
        log.debug(f"Fast-Warmup fehlgeschlagen: {e}")
