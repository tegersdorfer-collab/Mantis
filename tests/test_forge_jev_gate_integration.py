"""Integration tests for the Jev preflight in the Forge daemon."""

from forge import daemon as d
from forge import jev_gate
from forge import models as m


def _task(task_id=1):
    return {"id": task_id, "title": "Test", "description": "Ändere Code", "state": m.SPECCING}


def test_blocked_preflight_parks_before_pipeline(monkeypatch, tmp_path):
    task = _task()
    monkeypatch.setattr(d.queue, "claim_next", lambda: task)
    monkeypatch.setattr(d.worktree, "create", lambda task_id, **kwargs: tmp_path)
    monkeypatch.setattr(
        d.jev_gate,
        "preflight",
        lambda task, worktree: jev_gate.PreflightResult(
            status="blocked", mode="plan", risk="critical", external=True,
            reason="externe Absicht erkannt",
        ),
    )
    pipeline_calls = []
    monkeypatch.setattr(d.pipeline, "eine_stufe", lambda *args: pipeline_calls.append(args) or "weiter")
    parks = []
    monkeypatch.setattr(d.queue, "zaehle_fehlschlag", lambda *args, **kwargs: False)
    monkeypatch.setattr(d.queue, "park", lambda *args, **kwargs: parks.append((args, kwargs)) or True)
    monkeypatch.setattr(d.journal, "log", lambda *args, **kwargs: None)

    assert d.tick() == "geparkt"
    assert pipeline_calls == []
    assert "externe Absicht erkannt" in parks[0][1]["reason"]


def test_ready_preflight_reicht_nur_kontext_an_pipeline_weiter(monkeypatch, tmp_path):
    task = _task(2)
    monkeypatch.setattr(d.queue, "claim_next", lambda: task)
    monkeypatch.setattr(d.worktree, "create", lambda task_id, **kwargs: tmp_path)
    result = jev_gate.PreflightResult(status="ready", mode="security_review", risk="high")
    monkeypatch.setattr(d.jev_gate, "preflight", lambda task, worktree: result)
    erhalten = []
    monkeypatch.setattr(d.pipeline, "eine_stufe", lambda task, worktree: erhalten.append(task) or "weiter")

    assert d.tick() == "weiter"
    assert erhalten[0]["_jev_gate"] == result.as_context()

