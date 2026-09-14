"""Generated Python is review data, never executable application code."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import skill_factory as factory
from core import tools as T
import domains.dynamic_skills as dynamic_package


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, 'SKILLS_DIR', tmp_path)
    monkeypatch.setattr(dynamic_package, '__path__', [str(tmp_path)])
    monkeypatch.setattr(factory.db, 'get_setting', lambda *args: [])
    monkeypatch.setattr(factory.db, 'set_setting', lambda *args: None)
    monkeypatch.setattr(factory.db, 'log_event', lambda *args: None)
    # Keep regression runs safe even against the historical auto-commit code.
    monkeypatch.setattr(factory, '_git_commit', lambda *args: '', raising=False)
    yield tmp_path
    T.REGISTRY.pop('review_probe', None)
    sys.modules.pop('domains.dynamic_skills.review_probe', None)


def source(marker):
    # Accepted by the old whitelist: re-exported capabilities bypass import bans.
    return (
        'from core.skill_factory import Path\n'
        f'Path({str(marker)!r}).write_text("executed")\n'
        '@T.register("review_probe", "probe", {}, [], "general")\n'
        'async def review_probe():\n    return "ok"\n'
    )


def test_create_saves_review_artifact_without_executing(skill_dir):
    marker = skill_dir / 'executed.txt'
    result = factory.create_skill('review_probe', 'Review this', source(marker))
    assert not marker.exists(), 'Generated top-level code executed in application process'
    assert result['ok'] is True
    assert result['active'] is False
    assert result['status'] == 'pending_review'
    assert 'review_probe' not in T.REGISTRY
    assert not (skill_dir / 'review_probe.py').exists()
    assert (skill_dir / 'review_probe.py.pending').read_text().endswith(source(marker))
    assert factory.list_dynamic_skills() == ['review_probe']


def test_startup_never_imports_legacy_generated_python(skill_dir):
    marker = skill_dir / 'executed.txt'
    (skill_dir / 'review_probe.py').write_text('from core import tools as T\n' + source(marker))
    assert factory.load_all_on_startup() == 0
    assert not marker.exists()
    assert 'review_probe' not in T.REGISTRY


def test_delete_cannot_escape_skill_directory(skill_dir):
    victim = skill_dir.parent / 'victim.py'
    victim.write_text('preserve me')
    result = factory.delete_skill('../victim')
    assert result['ok'] is False
    assert victim.read_text() == 'preserve me'


def test_pending_artifact_can_be_deleted(skill_dir):
    artifact = skill_dir / 'review_probe.py.pending'
    artifact.write_text('review data')
    assert factory.delete_skill('review_probe')['ok'] is True
    assert not artifact.exists()


def test_description_is_only_a_comment(skill_dir):
    text = '\"\"\"\nraise RuntimeError("injected")\n#'
    result = factory.create_skill('review_probe', text, source(skill_dir / 'marker'))
    assert result['ok'] is True
    artifact = (skill_dir / 'review_probe.py.pending').read_text()
    assert '\nraise RuntimeError' not in artifact
