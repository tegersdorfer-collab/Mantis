"""Exercise learned procedure persistence and recall without external services."""
import asyncio
import time

import pytest

from core import background_review, skill_md
import config


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(background_review, 'SKILLS_MD_DIR', tmp_path)
    monkeypatch.setattr(skill_md, 'SKILLS_MD_DIR', tmp_path)
    monkeypatch.setattr(skill_md, '_INDEX', {})
    # Keep the automatic scan interval closed: writes must refresh recall directly.
    monkeypatch.setattr(skill_md, '_LAST_SCAN', time.time())
    monkeypatch.setattr(config, 'BACKGROUND_REVIEW_ENABLED', True, raising=False)
    return tmp_path


class ReviewResponse:
    def __init__(self, response):
        self.response = response

    async def chat(self, **kwargs):
        return self.response

    async def embed(self, text):
        raise AssertionError('Skill learning must not invoke memory embedding')


class NoMemory:
    def save(self, *args):
        raise AssertionError('Skill learning must not persist unrelated memory')


def learn(text):
    asyncio.run(background_review.run_background_review(
        'Remember this procedure', 'Procedure completed', ['get_notes', 'create_task'],
        ReviewResponse(text), NoMemory(),
    ))


@pytest.mark.parametrize('as_dict', [False, True])
def test_learning_persists_procedure_and_recalls_after_index_rebuild(skills_dir, as_dict):
    response = (
        'SAVE_SKILL: weekly_plan\n---\nname: weekly_plan\n'
        'description: Plan the weekly work\ntriggers: [weeklyplanning]\n---\n'
        '1. Read open tasks.\n2. Order tasks by deadline.'
    )
    learn({'content': response} if as_dict else response)

    artifact = skills_dir / 'weekly_plan.md'
    assert artifact.is_file()
    assert 'created_by: background_review' in artifact.read_text()
    assert '1. Read open tasks.' in skill_md.build_skill_context('weeklyplanning')
    # Rebuild from disk, as on a fresh process; cached output is not sufficient.
    skill_md._INDEX.clear()
    assert skill_md.scan_all() == 1
    context = skill_md.build_skill_context('weeklyplanning')
    assert 'weekly_plan: Plan the weekly work' in context
    assert '2. Order tasks by deadline.' in context
    assert skill_md.build_skill_context('zzzzzz') == ''


def test_learning_update_replaces_cached_body_and_trigger_immediately(skills_dir):
    learn(
        'SAVE_SKILL: planning\n---\nname: planning\n'
        'description: Planning\ntriggers: [obsoletekeyword]\n---\n'
        'Old procedure.'
    )
    assert 'Old procedure.' in skill_md.build_skill_context('obsoletekeyword')

    learn(
        'SAVE_SKILL: planning\n---\nname: planning\n'
        'description: Updated planning\ntriggers: [freshkeyword]\n---\n'
        'New procedure.'
    )
    assert skill_md.build_skill_context('obsoletekeyword') == ''
    context = skill_md.build_skill_context('freshkeyword')
    assert 'New procedure.' in context
    assert 'Old procedure.' not in context
    assert len(list(skills_dir.glob('*.md'))) == 1
    assert 'New procedure.' in (skills_dir / 'planning.md').read_text()


def test_disabled_background_review_does_not_call_llm(monkeypatch):
    class MustNotRun:
        calls = 0

        async def chat(self, **_kwargs):
            self.calls += 1
            return 'SKIP'

    monkeypatch.setattr(config, 'BACKGROUND_REVIEW_ENABLED', False, raising=False)
    llm = MustNotRun()

    asyncio.run(background_review.run_background_review(
        'input', 'response', [], llm, NoMemory(),
    ))

    assert llm.calls == 0


def test_message_handler_skips_review_task_but_keeps_fact_extraction(monkeypatch):
    from types import SimpleNamespace

    from core import message_handler
    from core.message_handler import MessageHandler

    extracted = []
    scheduled = []

    class Extractor:
        async def extract_from_exchange(self, user_text, response):
            extracted.append((user_text, response))
            return 1

    handler = object.__new__(MessageHandler)
    handler.extractor = Extractor()
    handler.bg_llm = SimpleNamespace()
    handler.lzg = SimpleNamespace()
    monkeypatch.setattr(config, 'BACKGROUND_REVIEW_ENABLED', False, raising=False)
    monkeypatch.setattr(message_handler.BUS, 'emit', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        message_handler,
        '_safe_task',
        lambda coro, label: scheduled.append(label),
    )

    asyncio.run(handler._post_turn('user fact', 'answer', []))

    assert extracted == [('user fact', 'answer')]
    assert scheduled == []
