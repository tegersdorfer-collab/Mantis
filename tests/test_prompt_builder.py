import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.prompt_builder import (
    CacheableSystemPrompt, PromptBuilder, _is_recallless_acknowledgement,
)


@pytest.mark.parametrize(("text", "expected"), [
    ("ja, und erinner mich an X", False),
    ("ja bitte schreib Lisa", False),
    ("nein danke", False),
    ("ja ja", False),
    ("Ja.", True),
    ("ja!", True),
    ("ok", True),
])
def test_recallless_acknowledgement_requires_a_bare_confirmation(text, expected):
    assert _is_recallless_acknowledgement(text) is expected


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
    monkeypatch.setattr("core.situation.prompt_context", lambda: "SITUATION_CONTEXT")

    prompt = asyncio.run(builder.build("frage"))

    assert isinstance(prompt, CacheableSystemPrompt)
    assert str(prompt).startswith(prompt.cache_prefix)
    dynamic = str(prompt)[len(prompt.cache_prefix):]
    assert all(value in dynamic for value in (
        "WARM_PROFILE_CONTEXT", "DIRECTIVES_CONTEXT", "SKILL_CONTEXT", "BEHAVIOR_CONTEXT",
        "MEMORY_CONTEXT", "KG_CONTEXT", "TASK_CONTEXT", "DASHBOARD_CONTEXT", "## Aktuell:",
        "SITUATION_CONTEXT",
    ))
    assert "SITUATION_CONTEXT" not in prompt.cache_prefix
    assert prompt.cache_prefix == (
        "IDENTITY\n\n## Werkzeuge\n"
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
    extended = prompt + "\n\nVoice instructions"
    assert extended.cache_prefix == prompt.cache_prefix
    assert extended.endswith("Voice instructions")


def test_prompt_builder_skips_long_term_memory_embedding_for_bare_acknowledgement():
    embed = AsyncMock(return_value=[0.1, 0.2])
    searches = []
    builder = PromptBuilder(
        embed_llm=SimpleNamespace(embed=embed),
        lzg=SimpleNamespace(
            search_hybrid=lambda *args, **kwargs: searches.append((args, kwargs)) or [],
            format_for_context=lambda _memories: "MEMORY_CONTEXT",
        ),
        kzg=SimpleNamespace(get_recent_turns=lambda **_kwargs: []),
        kg=SimpleNamespace(
            format_for_context=lambda: "",
            format_directives=lambda: "",
            warm_profile=lambda: "",
        ),
        reflection=SimpleNamespace(behavior_notes=lambda: ""),
        dashboard=SimpleNamespace(format_for_context=lambda: ""),
        forgetting=SimpleNamespace(bump_recall=lambda _memory_id: None),
        identity="IDENTITY",
    )
    builder._task_ctx = lambda: ""

    asyncio.run(builder.build("ja"))

    assert embed.await_count == 0
    assert searches == []


def test_speculative_prompt_build_stages_recall_without_mutating_memory_state(monkeypatch):
    recalled = []
    memory = SimpleNamespace(id=17)
    builder = PromptBuilder(
        embed_llm=SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2])),
        lzg=SimpleNamespace(
            search_hybrid=lambda *_args, **_kwargs: [memory],
            format_for_context=lambda memories: ", ".join(str(item.id) for item in memories),
        ),
        kzg=SimpleNamespace(get_recent_turns=lambda **_kwargs: []),
        kg=SimpleNamespace(
            format_for_context=lambda: "",
            format_directives=lambda: "",
            warm_profile=lambda: "",
        ),
        reflection=SimpleNamespace(behavior_notes=lambda: ""),
        dashboard=SimpleNamespace(format_for_context=lambda: ""),
        forgetting=SimpleNamespace(bump_recall=recalled.append),
        identity="IDENTITY",
    )
    builder._last_mem_ids = [99]
    builder._task_ctx = lambda: ""
    monkeypatch.setattr("core.prompt_builder.skill_md.build_skill_context", lambda _text: "")
    monkeypatch.setattr("core.situation.prompt_context", lambda: "")

    prompt = asyncio.run(builder.build("spekulative Frage", speculative=True))

    assert "17" in prompt
    assert prompt.recall_ids == (17,)
    assert recalled == []
    assert builder._last_mem_ids == [99]

    builder.book_recall(prompt.recall_ids)

    assert recalled == [17]
    assert builder._last_mem_ids == [17]
