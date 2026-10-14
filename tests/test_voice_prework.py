import asyncio

from core.voice_prework import VoicePreworkCache, VoiceTurnPrework, partial_text_matches_final


def make_prework(text):
    return VoiceTurnPrework(
        text=text,
        system=f"Kontext für: {text}",
        keyword_tools=("get_weather",),
        force_tools=False,
        elapsed_ms=42.5,
    )


def test_partial_text_may_equal_final_or_be_prefix_with_at_most_three_words():
    assert partial_text_matches_final("Wie spät ist es?", "wie spät ist es")
    assert partial_text_matches_final("Mach das Licht an", "Mach das Licht an, bitte jetzt")
    assert not partial_text_matches_final("Mach das Licht an", "Mach das Licht an und schalte den Ventilator ein")
    assert not partial_text_matches_final("Wie spät ist", "Wie ist spät")


def test_prework_cache_returns_only_work_for_latest_matching_partial():
    cache = VoicePreworkCache(make_prework)

    async def run():
        cache.offer("Wie spät ist es")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cache.offer("Wie spät ist es gerade")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        reused = cache.take_for("Wie spät ist es gerade?")
        stale = cache.take_for("Wie spät ist es nächste Woche")
        return reused, stale

    reused, stale = asyncio.run(run())

    assert reused is not None
    assert reused.text == "Wie spät ist es gerade"
    assert reused.system == "Kontext für: Wie spät ist es gerade"
    assert stale is None


def test_prework_cache_keeps_only_newest_pending_text_while_build_is_busy():
    started = asyncio.Event()
    release = asyncio.Event()
    built = []

    async def prepare(text):
        built.append(text)
        if text == "Erinner mich":
            started.set()
            await release.wait()
        return make_prework(text)

    async def run():
        cache = VoicePreworkCache(prepare)
        cache.offer("Erinner mich")
        await started.wait()
        cache.offer("Erinner mich morgen")
        cache.offer("Erinner mich morgen an den Termin")
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return cache, built

    cache, built = asyncio.run(run())

    assert built == ["Erinner mich", "Erinner mich morgen an den Termin"]
    assert cache.take_for("Erinner mich morgen an den Termin") is not None


def test_partial_prework_uses_local_selection_and_context_without_cloud_or_agent(monkeypatch):
    from core import message_handler

    handler = object.__new__(message_handler.MessageHandler)
    handler.prompt_builder = type("PromptBuilder", (), {
        "build": lambda _self, text: asyncio.sleep(0, result=f"Kontext: {text}"),
    })()
    cloud_calls = []

    def no_cloud(*args, **kwargs):
        cloud_calls.append((args, kwargs))
        raise AssertionError("Teiltext darf keine Cloud-Klassifikation auslösen")

    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", no_cloud)

    async def run():
        return await handler.prepare_voice_turn("Erinner mich morgen")

    prework = asyncio.run(run())

    assert prework.text == "Erinner mich morgen"
    assert prework.system == "Kontext: Erinner mich morgen"
    assert "set_reminder" in prework.keyword_tools
    assert cloud_calls == []


def test_agent_turn_reuses_matching_context_but_selects_tools_from_final_text(monkeypatch):
    from core import message_handler

    handler = object.__new__(message_handler.MessageHandler)
    builds = []

    class PromptBuilder:
        async def build(self, text):
            builds.append(text)
            return f"Kontext: {text}"

    class KZG:
        def recent_messages(self, **kwargs):
            return [{"role": "user", "content": "final"}]

    class Agent:
        backend = type("Backend", (), {"sends_full_tool_catalog": True})()

        async def run(self, **kwargs):
            self.kwargs = kwargs
            self.final_selection = await kwargs["tool_selection_task"]
            return "Erledigt.", []

    handler.prompt_builder = PromptBuilder()
    handler.kzg = KZG()
    agent = Agent()
    selections = []

    def keyword_selection(text):
        selections.append(("keyword", text))
        return ["keyword-final"], False

    async def async_selection(text):
        selections.append(("async", text))
        return ["cloud-final"], False

    monkeypatch.setattr(message_handler, "_keyword_tool_selection", keyword_selection)
    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", async_selection)
    monkeypatch.setattr(message_handler.fast_commands, "match_voice", lambda _text: None)
    monkeypatch.setattr(message_handler.skill_request, "match", lambda _text: None)

    partial = VoiceTurnPrework(
        text="Was steht heute an",
        system="Vorgewärmter Kontext",
        keyword_tools=("partial-tool",),
        force_tools=False,
        elapsed_ms=60.0,
    )

    async def run():
        return await handler._agent_turn(
            "Was steht heute an, bitte",
            channel_name="voice",
            agent=agent,
            prework=partial,
        )

    asyncio.run(run())

    assert builds == []
    assert "Vorgewärmter Kontext" in agent.kwargs["system"]
    assert agent.kwargs["allowed_tools"] == ["keyword-final"]
    assert agent.final_selection == (["cloud-final"], False)
    assert selections == [
        ("keyword", "Was steht heute an, bitte"),
        ("async", "Was steht heute an, bitte"),
    ]


def test_agent_turn_discards_prework_for_distant_final_text(monkeypatch):
    from core import message_handler

    handler = object.__new__(message_handler.MessageHandler)
    builds = []

    class PromptBuilder:
        async def build(self, text):
            builds.append(text)
            return f"Kontext: {text}"

    class KZG:
        def recent_messages(self, **kwargs):
            return []

    class Agent:
        backend = type("Backend", (), {"sends_full_tool_catalog": False})()

        async def run(self, **kwargs):
            self.kwargs = kwargs
            return "Antwort.", []

    handler.prompt_builder = PromptBuilder()
    handler.kzg = KZG()
    agent = Agent()
    monkeypatch.setattr(message_handler, "_keyword_tool_selection", lambda _text: ([], False))
    monkeypatch.setattr(message_handler.skills.T, "select_tools_async", lambda _text: asyncio.sleep(0, result=([], False)))
    monkeypatch.setattr(message_handler.fast_commands, "match_voice", lambda _text: None)
    monkeypatch.setattr(message_handler.skill_request, "match", lambda _text: None)
    partial = make_prework("Was steht heute an")

    asyncio.run(handler._agent_turn(
        "Erinnere mich in drei Wochen an einen Termin",
        channel_name="voice",
        agent=agent,
        prework=partial,
    ))

    assert builds == ["Erinnere mich in drei Wochen an einen Termin"]
    assert "Kontext: Erinnere mich in drei Wochen" in agent.kwargs["system"]
