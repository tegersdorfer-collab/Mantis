import asyncio

from memory.extractor import MemoryExtractor


class FakeFactProvider:
    def __init__(self, answer="[]"):
        self.answer = answer
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.answer


class FakeLocalClient:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("Cloud-Aufgaben dürfen hier nicht Ollama aufrufen")


class FakeLZG:
    pass


def test_fact_extraction_uses_cloud_provider_and_only_sends_user_text(monkeypatch):
    import memory.extractor as extractor_module

    local_client = FakeLocalClient()
    monkeypatch.setattr(extractor_module._ollama, "AsyncClient", lambda **_: local_client)
    provider = FakeFactProvider()
    extractor = MemoryExtractor(
        llm_provider=object(),
        lzg=FakeLZG(),
        fact_llm=provider,
    )

    result = asyncio.run(extractor.extract_from_exchange("hallo", "Antwort mit Zusatz"))

    assert result == 0
    assert len(provider.calls) == 1
    prompt, kwargs = provider.calls[0]
    assert prompt.endswith("Timo sagt: hallo")
    assert "Antwort mit Zusatz" not in prompt
    assert kwargs["format"] == "json"
    assert local_client.calls == []


def test_entity_extraction_uses_cloud_provider(monkeypatch):
    import memory.extractor as extractor_module

    local_client = FakeLocalClient()
    monkeypatch.setattr(extractor_module._ollama, "AsyncClient", lambda **_: local_client)
    provider = FakeFactProvider()

    class FakeKG:
        def upsert_entity(self, *_args):
            return 1

    extractor = MemoryExtractor(
        llm_provider=object(),
        lzg=FakeLZG(),
        kg=FakeKG(),
        fact_llm=provider,
    )

    asyncio.run(extractor._extract_entities("hallo"))

    assert len(provider.calls) == 1
    assert "Nachricht: hallo" in provider.calls[0][0]
    assert local_client.calls == []
