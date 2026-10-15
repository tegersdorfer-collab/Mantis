#!/usr/bin/env python3
"""Manueller Vertex-AI-Gemini-Probe: Text, erzwungener Tool-Call und SSE."""
import asyncio
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from core.backends.gemini import GeminiBackend  # noqa: E402


TOOL = [{
    "type": "function",
    "function": {
        "name": "probe_tool",
        "description": "Gibt die empfangene Probe-Kennung zurück.",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
    },
}]


async def main() -> None:
    if not str(config.GEMINI_API_KEY or "").strip():
        raise SystemExit("GEMINI_API_KEY fehlt in der .env-Datei.")

    backend = GeminiBackend(purpose="chat-agent")
    try:
        start = time.perf_counter()
        text, _ = await backend.call(
            messages=[{"role": "user", "content": "Antworte knapp mit: Gemini bereit."}],
            tools=None,
            stream_cb=None,
            temperature=0.2,
            max_tokens=80,
        )
        print(f"Text: {text!r} | Latenz: {(time.perf_counter() - start) * 1000:.0f} ms")

        start = time.perf_counter()
        _, tool_calls = await backend.call(
            messages=[{"role": "user", "content": "Rufe probe_tool mit label 'vertex probe' auf."}],
            tools=TOOL,
            stream_cb=None,
            temperature=0.2,
            max_tokens=80,
            force_tool_call=True,
        )
        print(
            f"Tool-Call: {tool_calls!r} | Latenz: "
            f"{(time.perf_counter() - start) * 1000:.0f} ms"
        )

        start = time.perf_counter()
        first_token_at = None

        async def collect(_chunk: str) -> None:
            nonlocal first_token_at
            if first_token_at is None:
                first_token_at = time.perf_counter()

        streamed, _ = await backend.call(
            messages=[{"role": "user", "content": "Antworte in einem kurzen Satz über Gemini."}],
            tools=None,
            stream_cb=collect,
            temperature=0.2,
            max_tokens=80,
        )
        elapsed = time.perf_counter() - start
        ttft = f"{(first_token_at - start) * 1000:.0f} ms" if first_token_at else "kein Text-Chunk"
        print(f"Streaming: {streamed!r} | Latenz: {elapsed * 1000:.0f} ms | TTFT: {ttft}")
    finally:
        await backend._client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
