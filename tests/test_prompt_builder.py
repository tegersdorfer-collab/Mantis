import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.prompt_builder import CacheableSystemPrompt, PromptBuilder


def test_prompt_builder_marks_static_prefix_and_keeps_live_context_in_dynamic_suffix(monkeypatch):
    builder = PromptBuilder(
        embed_llm=SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2])),
        lzg=SimpleNamespace(
            search_hybrid=lambda *_args, **_kwargs: [],
            format_for_context=lambda _memories: "MEMORY_CONTEXT",
        ),
        kzg=SimpleNamespace(get_recent_turns=lambda **_kwargs: []),
        kg=SimpleNamespace(
            format_for_context=lambda: "KG_CONTEXT",
            format_directives=lambda: "DIRECTIVES_CONTEXT",
            warm_profile=lambda: "WARM_PROFILE_CONTEXT",
        ),
        reflection=SimpleNamespace(behavior_notes=lambda: "BEHAVIOR_CONTEXT"),
        dashboard=SimpleNamespace(format_for_context=lambda: "DASHBOARD_CONTEXT"),
        forgetting=SimpleNamespace(bump_recall=lambda _memory_id: None),
        identity="IDENTITY",
    )
    builder._task_ctx = lambda: "TASK_CONTEXT"
    monkeypatch.setattr("core.prompt_builder.skill_md.build_skill_context", lambda _text: "SKILL_CONTEXT")

    prompt = asyncio.run(builder.build("frage"))

    assert isinstance(prompt, CacheableSystemPrompt)
    assert str(prompt).startswith(prompt.cache_prefix)
    dynamic = str(prompt)[len(prompt.cache_prefix):]
    assert all(value in dynamic for value in (
        "WARM_PROFILE_CONTEXT", "DIRECTIVES_CONTEXT", "SKILL_CONTEXT", "BEHAVIOR_CONTEXT",
        "MEMORY_CONTEXT", "KG_CONTEXT", "TASK_CONTEXT", "DASHBOARD_CONTEXT", "## Aktuell:",
    ))
    extended = prompt + "\n\nVoice instructions"
    assert extended.cache_prefix == prompt.cache_prefix
    assert extended.endswith("Voice instructions")
