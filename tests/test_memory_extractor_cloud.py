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


def test_local_extractor_verifier_bypasses_jev(monkeypatch):
    import memory.extractor as extractor_module

    class LocalClient:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return type("Response", (), {
                "message": type("Message", (), {"content": "JA"})(),
            })()

    async def jev_must_not_run(*_args, **_kwargs):
        raise AssertionError("Lokale Journaldaten dürfen Jev nicht erreichen")

    client = LocalClient()
    monkeypatch.setattr(extractor_module.decisions, "claim_supported", jev_must_not_run)

    assert asyncio.run(extractor_module.verify_claim(
        client, "Ich wohne in Nürnberg", "Timo wohnt in Nürnberg.", allow_cloud=False
    ))
    assert len(client.calls) == 1


def test_local_extractor_conflict_judge_bypasses_jev(monkeypatch):
    import memory.conflict as conflict_module
    import memory.extractor as extractor_module

    async def local_judge(old, new):
        return old == "alter Fakt" and new == "neuer Fakt"

    async def jev_must_not_run(*_args, **_kwargs):
        raise AssertionError("Lokale LZG-Daten dürfen Jev nicht erreichen")

    monkeypatch.setattr(conflict_module, "make_llm_judge", lambda *_: local_judge)
    monkeypatch.setattr(extractor_module.decisions, "supersedes", jev_must_not_run)
    judge = extractor_module.make_judge(object(), "local-model", allow_cloud=False)

    assert asyncio.run(judge("alter Fakt", "neuer Fakt"))
