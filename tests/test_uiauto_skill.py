"""Tests für den UI-Automatik-Skill (core/skills/uiauto.py).

Engine gemockt; geprüft werden: Safety-Gate im ui_click (Redline-Element wird
NICHT an die Engine gereicht), Fehlerbehandlung, und dass computer_task ohne
Bedienungshilfen-Recht sofort die Setup-Meldung liefert statt den qwen-Loop zu
starten.
"""

import asyncio
import os
import sys

import core.agent
import core.backends.ollama
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.uiauto import engine
from core.skills import uiauto
from core import uiauto_controller as controller
from core.uiauto_controller import ControllerResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _restore_engine_functions():
    """Keep legacy direct engine doubles from leaking into other test modules."""
    names = ("is_trusted", "snapshot", "element", "act", "type_text", "press_key")
    original = {name: getattr(engine, name) for name in names}
    yield
    for name, value in original.items():
        setattr(engine, name, value)


# ── ui_inspect ────────────────────────────────────────────────────────────────

def test_ui_inspect_formats_elements():
    engine.snapshot = lambda app=None: [
        {"ref": 0, "role": "AXButton", "title": "OK", "value": "", "enabled": True},
        {"ref": 1, "role": "AXTextField", "title": "Suche", "value": "hi", "enabled": True},
    ]
    out = _run(uiauto._ui_inspect("Notizen"))
    assert "ref 0" in out and "OK" in out
    assert "ref 1" in out and "Suche" in out


def test_ui_inspect_reports_permission_error():
    def boom(app=None):
        raise engine.UIAutoError("Kein Bedienungshilfen-Recht.")
    engine.snapshot = boom
    out = _run(uiauto._ui_inspect(""))
    assert "❌" in out and "recht" in out.lower()


# ── ui_click Safety-Gate ──────────────────────────────────────────────────────

def test_ui_click_blocks_redline():
    engine.element = lambda ref: {"ref": ref, "role": "AXButton", "title": "Löschen",
                                   "value": "", "enabled": True}
    acted = []
    engine.act = lambda ref, action="AXPress": acted.append(ref)
    out = _run(uiauto._ui_click(3))
    assert "⛔" in out
    assert acted == []          # Engine wurde NICHT aufgerufen


def test_ui_click_blocks_secure_field():
    engine.element = lambda ref: {"ref": ref, "role": "AXSecureTextField", "title": "Passwort",
                                  "value": "", "enabled": True}
    acted = []
    engine.act = lambda ref, action="AXPress": acted.append(ref)
    out = _run(uiauto._ui_click(0))
    assert "⛔" in out and acted == []


def test_ui_click_performs_normal():
    engine.element = lambda ref: {"ref": ref, "role": "AXButton", "title": "Weiter",
                                  "value": "", "enabled": True}
    acted = []
    engine.act = lambda ref, action="AXPress": acted.append(ref)
    out = _run(uiauto._ui_click(2))
    assert acted == [2] and ("✓" in out or "geklickt" in out.lower())


def test_ui_click_invalid_ref():
    engine.element = lambda ref: None
    out = _run(uiauto._ui_click(99))
    assert "❌" in out


def test_ui_type_and_key_delegate():
    calls = []
    engine.type_text = lambda t: calls.append(("type", t))
    engine.press_key = lambda c: calls.append(("key", c))
    _run(uiauto._ui_type("hallo"))
    _run(uiauto._ui_key("cmd+k"))
    assert calls == [("type", "hallo"), ("key", "cmd+k")]


# ── computer_task ─────────────────────────────────────────────────────────────

def test_computer_task_without_permission_gives_setup(monkeypatch):
    engine.is_trusted = lambda: False
    built = []
    monkeypatch.setattr(
        uiauto,
        "_run_ui_agent",
        lambda goal, app: built.append(1) or "sollte nicht laufen",
    )
    out = _run(uiauto._computer_task("irgendwas"))
    assert "bedienungshilfe" in out.lower() or "recht" in out.lower()
    assert built == []          # qwen-Loop wurde NICHT gestartet


def test_computer_task_runs_agent_when_trusted(monkeypatch):
    engine.is_trusted = lambda: True

    async def fake_agent(goal, app):
        return f"Habe '{goal}' erledigt (app={app})."
    monkeypatch.setattr(uiauto, "_run_ui_agent", fake_agent)
    out = _run(uiauto._computer_task("Notiz öffnen", app="Notizen"))
    assert "erledigt" in out and "Notizen" in out


# ── Jev-Controller-Routing und qwen-Writer ──────────────────────────────────

def test_run_ui_agent_returns_terminal_controller_result_without_qwen(monkeypatch):
    async def direct_to_thread(fn, *args):
        return fn(*args)

    for status, text in (("completed", "Jev erledigt"), ("aborted", "Jev abgebrochen")):
        monkeypatch.setattr(
            uiauto.controller,
            "run",
            lambda *args, status=status, text=text: ControllerResult(status, text),
        )
        monkeypatch.setattr(uiauto.asyncio, "to_thread", direct_to_thread)

        def qwen_must_not_start(*args, **kwargs):
            raise AssertionError("terminal Jev result must not start qwen")

        monkeypatch.setattr(core.agent, "Agent", qwen_must_not_start)

        assert _run(uiauto._run_ui_agent("Open note", "Notes")) == text


def test_run_ui_agent_uses_existing_qwen_path_after_controller_fallback(monkeypatch):
    async def direct_to_thread(fn, *args):
        return fn(*args)

    monkeypatch.setattr(
        uiauto.controller,
        "run",
        lambda *args: ControllerResult("fallback", "Use normal UI", "decision unavailable"),
    )
    monkeypatch.setattr(uiauto.asyncio, "to_thread", direct_to_thread)

    class FakeBackend:
        def __init__(self, model):
            self.model = model

    calls = []

    class FakeAgent:
        def __init__(self, backend, max_steps):
            assert isinstance(backend, FakeBackend)
            assert max_steps == uiauto.UI_MAX_STEPS

        async def run(self, **kwargs):
            calls.append(kwargs)
            return "qwen fallback", []

    monkeypatch.setattr(core.backends.ollama, "OllamaBackend", FakeBackend)
    monkeypatch.setattr(core.agent, "Agent", FakeAgent)

    assert _run(uiauto._run_ui_agent("Open note", "Notes")) == "qwen fallback"
    assert calls == [{
        "messages": [{"role": "user", "content": "Open note Ziel-App: Notes."}],
        "system": uiauto._UI_SYSTEM,
        "allowed_tools": uiauto.UI_TOOLS,
        "force_tools": True,
        "temperature": 0.3,
        "max_tokens": 1200,
    }]


def test_offline_replay_reports_qwen_agent_calls_for_closed_choice_and_fallback(monkeypatch):
    """A two-step Jev route needs no ReAct Agent; fallback starts it once."""
    async def direct_to_thread(fn, *args):
        return fn(*args)

    class ReplayEngine:
        def __init__(self):
            self.snapshots = iter([
                [{"ref": 1, "role": "AXButton", "title": "Open", "value": "", "enabled": True}],
                [{"ref": 2, "role": "AXButton", "title": "Next", "value": "", "enabled": True}],
                [],
            ])
            self.elements = {
                1: {"ref": 1, "role": "AXButton", "title": "Open", "value": "", "enabled": True},
                2: {"ref": 2, "role": "AXButton", "title": "Next", "value": "", "enabled": True},
            }
            self.actions = []

        def snapshot(self, app):
            return next(self.snapshots)

        def element(self, ref):
            return self.elements.get(ref)

        def act(self, ref):
            self.actions.append(ref)

    replay_engine = ReplayEngine()
    decisions = iter(["click:1", "click:2", "done"])
    qwen_agent_calls = []

    class FakeBackend:
        def __init__(self, model):
            self.model = model

    class FakeAgent:
        def __init__(self, backend, max_steps):
            qwen_agent_calls.append(max_steps)

        async def run(self, **kwargs):
            return "qwen fallback", []

    monkeypatch.setattr(uiauto.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(controller, "engine", replay_engine)
    monkeypatch.setattr(
        controller.decisions,
        "ui_action",
        lambda state, criteria: type("Answer", (), {"value": next(decisions)})(),
    )
    monkeypatch.setattr(core.backends.ollama, "OllamaBackend", FakeBackend)
    monkeypatch.setattr(core.agent, "Agent", FakeAgent)

    assert _run(uiauto._run_ui_agent("Open then continue", "Notes")) == "UI task completed"
    assert replay_engine.actions == [1, 2]
    closed_choice_qwen_calls = len(qwen_agent_calls)

    monkeypatch.setattr(
        uiauto.controller,
        "run",
        lambda *args: ControllerResult("fallback", "Use normal UI", "replay fallback"),
    )
    assert _run(uiauto._run_ui_agent("Open then continue", "Notes")) == "qwen fallback"
    fallback_qwen_calls = len(qwen_agent_calls) - closed_choice_qwen_calls

    replay_counts = {
        "closed_choice_qwen_agent_calls": closed_choice_qwen_calls,
        "fallback_qwen_agent_calls": fallback_qwen_calls,
    }
    assert replay_counts == {
        "closed_choice_qwen_agent_calls": 0,
        "fallback_qwen_agent_calls": 1,
    }, replay_counts


def test_default_writer_uses_one_tool_free_qwen_call(monkeypatch):
    class FakeBackend:
        def __init__(self, model):
            self.model = model

    calls = []

    class FakeAgent:
        def __init__(self, backend, max_steps):
            assert isinstance(backend, FakeBackend)
            assert max_steps == 1

        async def run(self, **kwargs):
            calls.append(kwargs)
            return "Timo", []

    monkeypatch.setattr(core.backends.ollama, "OllamaBackend", FakeBackend)
    monkeypatch.setattr(core.agent, "Agent", FakeAgent)

    assert _run(uiauto._write_ui_text("Enter your name", {"elements": []})) == "Timo"
    assert len(calls) == 1
    assert calls[0]["use_tools"] is False
    assert calls[0]["allowed_tools"] == []
    assert calls[0]["force_tools"] is False
    assert calls[0]["max_tokens"] <= 160
