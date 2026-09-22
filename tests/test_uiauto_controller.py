from __future__ import annotations

from dataclasses import asdict

import core.uiauto_controller as controller
from core.uiauto_controller import (
    ActionPlan,
    ControllerResult,
    build_choice,
    build_state,
    parse_action,
)


def _element(ref=1, *, role="AXButton", title="Save", value="", enabled=True, visible=True):
    return {
        "ref": ref,
        "role": role,
        "title": title,
        "value": value,
        "enabled": enabled,
        "visible": visible,
    }


def test_build_state_has_bounded_shape_and_recent_history_only():
    state = build_state(
        "G" * 500,
        "A" * 500,
        12,
        [_element(title="T" * 500, value="V" * 500)],
        [f"action-{index}" for index in range(12)],
    )

    assert set(state) == {"goal", "app", "step", "elements", "history"}
    assert state["step"] == 12
    assert state["history"] == [f"action-{index}" for index in range(4, 12)]
    assert len(state["goal"]) <= 160
    assert len(state["app"]) <= 160
    assert len(state["history"][0]) <= 160
    assert len(state["elements"][0]["title"]) <= 160
    assert len(state["elements"][0]["value"]) <= 160


def test_build_state_serializes_only_bounded_data_and_redacts_text_values():
    raw_object = object()
    state = build_state(
        "goal",
        None,
        0,
        [
            _element(role="AXTextField", value="complete user input"),
            _element(ref=2, role="AXSecureTextField", value="password"),
            {"ref": raw_object, "role": "AXButton", "title": "OK", "enabled": True},
        ],
        [],
    )

    assert state["app"] == ""
    assert state["elements"][0]["value"] == "<redacted>"
    assert state["elements"][1]["value"] == "<redacted>"
    assert state["elements"][2]["ref"] == ""
    assert set(state["elements"][0]) == {
        "ref", "role", "title", "value", "enabled", "visible",
    }


def test_build_state_rejects_string_refs_instead_of_sending_unbounded_data_to_jev():
    state = build_state("goal", None, 0, [_element(ref="R" * 10_000)], [])

    assert state["elements"][0]["ref"] == ""
    assert build_choice([_element(ref="R" * 10_000)]).criteria == {
        "key:escape": "Select this exact safe controller action.",
        "done": "Select this exact safe controller action.",
        "abort": "Select this exact safe controller action.",
    }


def test_build_state_keeps_only_eight_items_without_materializing_history():
    class StreamingHistory:
        def __iter__(self):
            yield from (f"action-{index}" for index in range(10))

        def __len__(self):
            raise AssertionError("history must be consumed as a stream")

    state = build_state("goal", None, 0, [], StreamingHistory())

    assert state["history"] == [f"action-{index}" for index in range(2, 10)]


def test_build_choice_contains_only_safe_generated_actions():
    plan = build_choice([
        _element(ref=1),
        _element(ref=2, role="AXTextField", title="Name"),
        _element(ref=3, role="AXSecureTextField", title="Password"),
        _element(ref=4, title="Disabled", enabled=False),
    ])

    assert isinstance(plan, ActionPlan)
    assert plan.status == "ready"
    assert set(plan.criteria) == {
        "click:1",
        "click:2",
        "type_text",
        "key:return",
        "key:escape",
        "done",
        "abort",
    }
    assert "click:3" not in plan.criteria
    assert "click:4" not in plan.criteria


def test_build_choice_has_terminal_actions_for_empty_representable_snapshot():
    plan = build_choice([])

    assert plan.status == "ready"
    assert set(plan.criteria) == {"done", "abort"}
    assert "key:escape" not in plan.criteria


def test_build_choice_falls_back_instead_of_truncating_over_255_options():
    elements = [_element(ref=index) for index in range(254)]

    plan = build_choice(elements)

    assert plan.status == "fallback"
    assert plan.criteria == {}
    assert plan.reason == "too many UI actions"


def test_build_choice_excludes_disabled_and_secure_fields_from_all_actions():
    plan = build_choice([
        _element(ref=1, role="AXSecureTextField", enabled=True),
        _element(ref=2, role="AXTextField", enabled=False),
        _element(ref=3, role="AXButton", enabled=False),
    ])

    assert set(plan.criteria) == {"done", "abort"}


def test_parse_action_accepts_only_actions_from_the_generated_plan():
    plan = build_choice([_element(ref=7, role="AXTextField")])

    for action in ("click:7", "type_text", "key:return", "key:escape", "done", "abort"):
        assert parse_action(action, plan) == action

    for action in ("click:8", "click:7 ", "ui_click(7)", "key:tab", "type_text:secret", "wait"):
        assert parse_action(action, plan) is None


def test_parse_action_rejects_non_strings_and_unrepresentable_plans():
    plan = build_choice([_element(ref=1)])

    assert parse_action(None, plan) is None
    assert parse_action("click:1", ActionPlan("fallback", {})) is None


def test_parse_action_rejects_forged_criteria_even_when_plan_is_ready():
    forged = ActionPlan(
        "ready",
        {
            "wait": "forged",
            "ui_click(1)": "forged",
            "click:1": "forged",
        },
    )

    assert parse_action("wait", forged) is None
    assert parse_action("ui_click(1)", forged) is None
    assert parse_action("click:1", forged) == "click:1"


def test_controller_result_has_stable_status_text_and_reason_fields():
    result = ControllerResult("fallback", "Use the normal UI path", "too many UI actions")

    assert asdict(result) == {
        "status": "fallback",
        "text": "Use the normal UI path",
        "reason": "too many UI actions",
    }


class _Answer:
    def __init__(self, value):
        self.value = value


class _FakeEngine:
    def __init__(self, snapshots, elements=None):
        self.snapshots = iter(snapshots)
        self.elements = elements or {}
        self.actions = []
        self.resolved_refs = []

    def snapshot(self, app):
        return next(self.snapshots)

    def element(self, ref):
        self.resolved_refs.append(ref)
        return self.elements.get(ref)

    def act(self, ref):
        self.actions.append(("click", ref))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press_key(self, key):
        self.actions.append(("key", key))


def _install_fakes(monkeypatch, engine, actions):
    monkeypatch.setattr(controller, "engine", engine)
    monkeypatch.setattr(
        controller.decisions,
        "ui_action",
        lambda state, criteria: _Answer(next(actions)),
    )


def test_run_performs_one_click_per_iteration_and_reresolves_ref(monkeypatch):
    first = [_element(ref=3, title="Open")]
    second = [_element(ref=4, title="Next")]
    engine = _FakeEngine([first, second], {3: first[0], 4: second[0]})
    states = []

    def fake_decision(state, criteria):
        states.append(state)
        return _Answer(("click:3", "done")[len(states) - 1])

    monkeypatch.setattr(controller, "engine", engine)
    monkeypatch.setattr(controller.decisions, "ui_action", fake_decision)

    result = controller.run("Open it", "Notes", max_steps=2)

    assert result.status == "completed"
    assert engine.actions == [("click", 3)]
    assert engine.resolved_refs == [3]
    assert states[1]["history"] == ["click:3"]


def test_run_returns_completed_for_done_and_aborted_for_abort(monkeypatch):
    for action, expected in (("done", "completed"), ("abort", "aborted")):
        engine = _FakeEngine([[]])
        _install_fakes(monkeypatch, engine, iter([action]))

        result = controller.run("Stop", "Notes")

        assert result.status == expected
        assert engine.actions == []


def test_run_falls_back_for_low_confidence_invalid_or_failed_decisions(monkeypatch):
    for decision in (None, _Answer("click:999"), RuntimeError("provider down")):
        engine = _FakeEngine([[_element()]])

        def fake_decision(state, criteria, decision=decision):
            if isinstance(decision, Exception):
                raise decision
            return decision

        monkeypatch.setattr(controller, "engine", engine)
        monkeypatch.setattr(controller.decisions, "ui_action", fake_decision)

        result = controller.run("Continue", "Notes")

        assert result.status == "fallback"
        assert engine.actions == []


def test_run_aborts_redline_before_engine_action(monkeypatch):
    dangerous = _element(ref=2, title="Delete")
    engine = _FakeEngine([[dangerous]], {2: dangerous})
    _install_fakes(monkeypatch, engine, iter(["click:2"]))

    result = controller.run("Remove", "Notes")

    assert result.status == "aborted"
    assert result.reason == "destruktiv/riskant: ‚delete'"
    assert result.reason in result.text
    assert engine.actions == []


def test_run_falls_back_for_stale_or_disabled_refs(monkeypatch):
    for resolved in (None, _element(ref=1, enabled=False)):
        engine = _FakeEngine([[_element(ref=1)]], {1: resolved} if resolved else {})
        _install_fakes(monkeypatch, engine, iter(["click:1"]))

        result = controller.run("Continue", "Notes")

        assert result.status == "fallback"
        assert engine.actions == []


def test_run_type_text_calls_writer_once_then_existing_typing_routine(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Name")
    engine = _FakeEngine([[field], []], {7: field})
    _install_fakes(monkeypatch, engine, iter(["type_text", "done"]))
    writer_calls = []

    def writer(goal, state):
        writer_calls.append((goal, state))
        return "Timo"

    result = controller.run("Enter name", "Notes", writer=writer)

    assert result.status == "completed"
    assert len(writer_calls) == 1
    assert writer_calls[0][0] == "Enter name"
    assert writer_calls[0][1]["elements"][0]["value"] == "<redacted>"
    assert engine.actions == [("type", "Timo")]
    assert engine.resolved_refs == [7]


def test_run_falls_back_when_typing_or_engine_action_fails(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Name")
    engine = _FakeEngine([[field]], {7: field})
    _install_fakes(monkeypatch, engine, iter(["type_text"]))
    engine.type_text = lambda text: (_ for _ in ()).throw(RuntimeError("unavailable"))

    result = controller.run("Enter name", "Notes", writer=lambda goal, state: "Timo")

    assert result.status == "fallback"


def test_run_falls_back_without_valid_writer_text(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Name")
    for writer in (None, lambda goal, state: "", lambda goal, state: 42):
        engine = _FakeEngine([[field]], {7: field})
        _install_fakes(monkeypatch, engine, iter(["type_text"]))

        result = controller.run("Enter name", "Notes", writer=writer)

        assert result.status == "fallback"
        assert engine.actions == []


def test_run_falls_back_when_writer_fails(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Name")
    engine = _FakeEngine([[field]], {7: field})
    _install_fakes(monkeypatch, engine, iter(["type_text"]))

    def failing_writer(goal, state):
        raise RuntimeError("writer unavailable")

    result = controller.run("Enter name", "Notes", writer=failing_writer)

    assert result.status == "fallback"
    assert engine.actions == []


def test_run_aborts_return_for_a_redline_text_target(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Send")
    engine = _FakeEngine([[field]], {7: field})
    _install_fakes(monkeypatch, engine, iter(["key:return"]))

    result = controller.run("Reply", "Mail")

    assert result.status == "aborted"
    assert engine.actions == []
    assert engine.resolved_refs == [7]


def test_run_aborts_return_when_snapshot_has_visible_redline_action(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Message")
    send = _element(ref=8, title="Send")
    engine = _FakeEngine([[field, send]], {7: field, 8: send})
    _install_fakes(monkeypatch, engine, iter(["key:return"]))

    result = controller.run("Reply", "Mail")

    assert result.status == "aborted"
    assert engine.actions == []
    assert engine.resolved_refs == [7]


def test_run_allows_return_when_snapshot_has_hidden_redline_action(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Message")
    hidden_send = _element(ref=8, title="Send", visible=False)
    engine = _FakeEngine([[field, hidden_send], []], {7: field, 8: hidden_send})
    _install_fakes(monkeypatch, engine, iter(["key:return", "done"]))

    result = controller.run("Reply", "Mail")

    assert result.status == "completed"
    assert engine.actions == [("key", "return")]


def test_run_aborts_return_when_redline_visibility_is_unknown(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Message")
    send = _element(ref=8, title="Send")
    send.pop("visible")
    engine = _FakeEngine([[field, send]], {7: field, 8: send})
    _install_fakes(monkeypatch, engine, iter(["key:return"]))

    result = controller.run("Reply", "Mail")

    assert result.status == "aborted"
    assert engine.actions == []


def test_run_allows_return_for_a_safe_current_text_field(monkeypatch):
    field = _element(ref=7, role="AXTextField", title="Message")
    engine = _FakeEngine([[field], []], {7: field})
    _install_fakes(monkeypatch, engine, iter(["key:return", "done"]))

    result = controller.run("Continue", "Notes")

    assert result.status == "completed"
    assert engine.actions == [("key", "return")]
    assert engine.resolved_refs == [7]
