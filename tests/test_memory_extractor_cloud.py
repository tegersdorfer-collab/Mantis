"""Memory-Extraktion, Verifier und Konflikt-Judge verwenden die Cloud-Kette."""
import asyncio

from core.backends.base import AgentBackend
from memory.extractor import MemoryExtractor, make_judge, verify_claim


class FakeFactProvider:
    def __init__(self, answer="[]"):
        self.answer = answer
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.answer


class FakeLZG:
    pass


class FakeCloudBackend(AgentBackend):
    def __init__(self):
        self.calls = []

    @property
    def model_name(self):
        return "fake-cloud"

    async def call(self, messages, tools, stream_cb, temperature, max_tokens,
                   think=False, force_tool_call=False):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return "[]", []

    async def warmup(self):
        return None


def test_fact_extraction_sends_only_user_words_to_cloud_provider():
    provider = FakeFactProvider()
    extractor = MemoryExtractor(
        llm_provider=object(), lzg=FakeLZG(), fact_llm=provider,
    )

    result = asyncio.run(extractor.extract_from_exchange("hallo", "Antwort mit Zusatz"))

    assert result == 0
    assert len(provider.calls) == 1
    prompt, kwargs = provider.calls[0]
    assert prompt.endswith("Timo sagt: hallo")
    assert "Antwort mit Zusatz" not in prompt
    assert kwargs["format"] == "json"


def test_default_fact_extractor_uses_fast_cloud_chain(monkeypatch):
    import memory.extractor as extractor_module

    backend = FakeCloudBackend()
    purposes = []

    def cloud_chain(purpose, **kwargs):
        purposes.append((purpose, kwargs))
        return backend

    class LocalClient:
        async def chat(self, **kwargs):
            return type("Response", (), {"message": type("Message", (), {"content": "[]"})()})()

    if hasattr(extractor_module, "_ollama"):
        monkeypatch.setattr(extractor_module._ollama, "AsyncClient", lambda **_: LocalClient())

    monkeypatch.setattr(extractor_module, "cloud_chain", cloud_chain, raising=False)
    extractor = MemoryExtractor(llm_provider=object(), lzg=FakeLZG())

    result = asyncio.run(extractor._ask_json("Extract JSON", 70, 8.0))

    assert result == "[]"
    assert purposes == [("fast", {"reasoning": "low", "timeout": 8})]
    assert backend.calls[0]["tools"] is None
    assert backend.calls[0]["max_tokens"] == 70


def test_entity_extraction_uses_cloud_provider():
    provider = FakeFactProvider()

    class FakeKG:
        def upsert_entity(self, *_args):
            return 1

    extractor = MemoryExtractor(
        llm_provider=object(), lzg=FakeLZG(), kg=FakeKG(), fact_llm=provider,
    )

    asyncio.run(extractor._extract_entities("hallo"))

    assert len(provider.calls) == 1
    assert "Nachricht: hallo" in provider.calls[0][0]


def test_claim_verifier_uses_cloud_provider_without_jev_or_local_client():
    provider = FakeFactProvider("JA")

    assert asyncio.run(verify_claim(provider, "Ich wohne in Nürnberg", "Timo wohnt in Nürnberg."))
    assert len(provider.calls) == 1
    assert provider.calls[0][1]["max_tokens"] <= 12


def test_conflict_judge_uses_cloud_provider():
    provider = FakeFactProvider("JA")
    judge = make_judge(provider)

    assert asyncio.run(judge("alter Fakt", "neuer Fakt")) is True
    assert "alte aussage" in provider.calls[0][0].lower()
    assert "neue aussage" in provider.calls[0][0].lower()
