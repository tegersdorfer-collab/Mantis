from __future__ import annotations

from dataclasses import asdict

from core.uiauto_controller import (
    ActionPlan,
    ControllerResult,
    build_choice,
    build_state,
    parse_action,
)


def _element(ref=1, *, role="AXButton", title="Save", value="", enabled=True):
    return {
        "ref": ref,
        "role": role,
        "title": title,
        "value": value,
        "enabled": enabled,
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
    assert set(state["elements"][0]) == {"ref", "role", "title", "value", "enabled"}


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
