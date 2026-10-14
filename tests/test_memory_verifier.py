"""Memory-Verifier und Konflikt-Judge laufen über den Cloud-Provider."""
import asyncio

from memory import extractor as ex


class FakeProvider:
    def __init__(self, answer="JA", failure=None):
        self.answer = answer
        self.failure = failure
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.failure:
            raise self.failure
        return self.answer


def test_verify_claim_uses_cloud_provider_and_names_speaker():
    provider = FakeProvider("NEIN")

    result = asyncio.run(ex.verify_claim(provider, "Ich war beim Zahnarzt", "Timo hat Angst"))

    assert result is False
    prompt, kwargs = provider.calls[0]
    assert "Text stammt von Timo" in prompt
    assert "Ich war beim Zahnarzt" in prompt and "Timo hat Angst" in prompt
    assert kwargs == {"temperature": 0.0, "max_tokens": 8}


def test_verifier_prompt_uses_explicit_speaker_context():
    prompt = ex.verify_prompt("Ich war beim Zahnarzt", "Timo war beim Zahnarzt")
    assert "Timo" in prompt and "Ich-Form" in prompt
    assert prompt.startswith("Antworte NUR mit JA oder NEIN")


def test_verify_claim_cloud_failure_is_conservative():
    provider = FakeProvider(failure=RuntimeError("offline"))

    assert asyncio.run(ex.verify_claim(provider, "Ich wohne hier", "Timo wohnt hier")) is False
    assert len(provider.calls) == 1


def test_judge_uses_cloud_provider():
    provider = FakeProvider("JA")
    judge = ex.make_judge(provider)

    assert asyncio.run(judge("wohnt in Leipzig", "nach Halle gezogen")) is True
    assert "alte aussage" in provider.calls[0][0].lower()
    assert "neue aussage" in provider.calls[0][0].lower()
