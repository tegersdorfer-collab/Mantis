"""Pure state and closed-choice helpers for the Jev UI controller."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections import deque
import inspect
import logging
import re
from typing import Callable, Iterable, Literal, Mapping

from core import decisions
from tools.uiauto import engine, safety
from tools.uiauto.engine import ACTIONABLE_ROLES
from tools.uiauto.safety import is_secure_field


MAX_HISTORY = 8
MAX_STRING_LENGTH = 160
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea"})
MAX_CHOICE_OPTIONS = 255
UI_MAX_STEPS = 12
_FALLBACK_TEXT = "Use the normal UI path"
_COMPLETED_TEXT = "UI task completed"
_ABORTED_TEXT = "UI action aborted"

log = logging.getLogger("core.uiauto_controller")


@dataclass(frozen=True)
class ControllerResult:
    status: Literal["completed", "aborted", "fallback"]
    text: str
    reason: str = ""


@dataclass(frozen=True)
class ActionPlan:
    status: Literal["ready", "fallback"]
    criteria: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""


def _bounded_string(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:MAX_STRING_LENGTH]


def _safe_ref(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _serialize_element(element: Mapping[str, object]) -> dict[str, object]:
    role = _bounded_string(element.get("role"))
    secure_or_text = role in TEXT_ROLES or is_secure_field({"role": role})
    ref = _safe_ref(element.get("ref"))
    return {
        "ref": ref if ref is not None else "",
        "role": role,
        "title": _bounded_string(element.get("title")),
        "value": "<redacted>" if secure_or_text else _bounded_string(element.get("value")),
        "enabled": bool(element.get("enabled", False)),
        "visible": _is_visible(element),
    }


def build_state(
    goal: str,
    app: str | None,
    step: int,
    elements: Iterable[Mapping[str, object]],
    history: Iterable[str],
) -> dict[str, object]:
    """Build the bounded, data-only state passed to the Jev adapter."""
    bounded_history: deque[str] = deque(maxlen=MAX_HISTORY)
    for item in history:
        bounded_history.append(_bounded_string(item))

    return {
        "goal": _bounded_string(goal),
        "app": _bounded_string(app),
        "step": step if isinstance(step, int) and not isinstance(step, bool) else 0,
        "elements": [_serialize_element(element) for element in elements],
        "history": list(bounded_history),
    }


def _usable(element: Mapping[str, object]) -> bool:
    return (
        bool(element.get("enabled", False))
        and element.get("role") in ACTIONABLE_ROLES
        and not is_secure_field(dict(element))
    )


def _is_visible(element: Mapping[str, object]) -> bool:
    visible = element.get("visible", True)
    return visible if isinstance(visible, bool) else True


def _action_ref(element: Mapping[str, object]) -> str | None:
    ref = _safe_ref(element.get("ref"))
    if ref is None:
        return None
    return str(ref)


def build_choice(elements: Iterable[Mapping[str, object]]) -> ActionPlan:
    """Create the closed action set for one representable UI snapshot."""
    usable = [element for element in elements if _usable(element)]
    actions: list[str] = []
    for element in usable:
        ref = _action_ref(element)
        if ref is not None:
            actions.append(f"click:{ref}")

    has_text_field = any(element.get("role") in TEXT_ROLES for element in usable)
    if has_text_field:
        actions.extend(["type_text", "key:return"])
    if usable:
        actions.append("key:escape")
    actions.extend(["done", "abort"])

    if len(actions) > MAX_CHOICE_OPTIONS:
        return ActionPlan("fallback", reason="too many UI actions")

    return ActionPlan(
        "ready",
        {action: "Select this exact safe controller action." for action in actions},
    )


def parse_action(action: object, plan: ActionPlan) -> str | None:
    """Accept only an exact action generated for the current choice plan."""
    if plan.status != "ready" or not isinstance(action, str):
        return None
    if action not in plan.criteria:
        return None
    if action in {"type_text", "key:return", "key:escape", "done", "abort"}:
        return action
    if re.fullmatch(r"click:[0-9]+", action):
        return action
    return None


def _fallback(reason: str) -> ControllerResult:
    log.warning("Jev UI controller fallback: %s", reason)
    return ControllerResult("fallback", _FALLBACK_TEXT, reason)


def _aborted(reason: str = "") -> ControllerResult:
    text = _ABORTED_TEXT if not reason else f"{_ABORTED_TEXT}: {reason}"
    return ControllerResult("aborted", text, reason)


def _decision_action(state: dict[str, object], plan: ActionPlan) -> object:
    answer = decisions.ui_action(state, dict(plan.criteria))
    if inspect.isawaitable(answer):
        return asyncio.run(answer)
    return answer


def _current_text_field(elements: Iterable[Mapping[str, object]]) -> int | None:
    for element in elements:
        ref = _safe_ref(element.get("ref"))
        if (
            ref is not None
            and element.get("role") in TEXT_ROLES
            and bool(element.get("enabled", False))
            and not is_secure_field(dict(element))
        ):
            return ref
    return None


def _valid_text_field(ref: int) -> bool:
    element = engine.element(ref)
    return bool(
        element
        and element.get("role") in TEXT_ROLES
        and element.get("enabled", False)
        and not is_secure_field(element)
    )


def _visible_redline_reason(elements: Iterable[Mapping[str, object]]) -> str:
    for element in elements:
        if (
            not bool(element.get("enabled", False))
            or not _is_visible(element)
            or element.get("role") not in ACTIONABLE_ROLES
        ):
            continue
        redline, reason = safety.is_redline(dict(element))
        if redline:
            return reason
    return ""


def run(
    goal: str,
    app: str | None,
    max_steps: int = UI_MAX_STEPS,
    writer: Callable[[str, Mapping[str, object]], str] | None = None,
) -> ControllerResult:
    """Execute one closed Jev UI decision per bounded synchronous iteration."""
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        return _fallback("invalid step limit")

    history: list[str] = []
    for step in range(max_steps):
        try:
            elements = engine.snapshot(app)
        except Exception:
            return _fallback("snapshot failed")

        state = build_state(goal, app, step, elements, history)
        plan = build_choice(elements)
        if plan.status != "ready":
            return _fallback(plan.reason or "no safe UI actions")

        try:
            answer = _decision_action(state, plan)
        except Exception:
            return _fallback("decision provider failed")
        action = parse_action(getattr(answer, "value", None), plan)
        if action is None:
            return _fallback("invalid or low-confidence decision")
        if action == "done":
            return ControllerResult("completed", _COMPLETED_TEXT)
        if action == "abort":
            return ControllerResult("aborted", _ABORTED_TEXT)

        try:
            if action.startswith("click:"):
                ref = int(action.removeprefix("click:"))
                element = engine.element(ref)
                if (
                    element is None
                    or not _usable(element)
                    or _action_ref(element) != str(ref)
                ):
                    return _fallback("stale or disabled UI reference")
                redline, reason = safety.is_redline(element)
                if redline:
                    return _aborted(reason)
                engine.act(ref)
            elif action == "type_text":
                ref = _current_text_field(elements)
                if ref is None or not _valid_text_field(ref):
                    return _fallback("no current non-secure text field")
                if writer is None:
                    return _fallback("no text writer")
                text = writer(goal, state)
                if not isinstance(text, str) or not text.strip():
                    return _fallback("writer returned invalid text")
                engine.type_text(text)
            elif action == "key:return":
                ref = _current_text_field(elements)
                element = engine.element(ref) if ref is not None else None
                if (
                    element is None
                    or element.get("role") not in TEXT_ROLES
                    or not bool(element.get("enabled", False))
                    or is_secure_field(element)
                ):
                    return _fallback("no current non-secure text field")
                redline, reason = safety.is_redline(element)
                if redline:
                    return _aborted(reason)
                visible_redline = _visible_redline_reason(elements)
                if visible_redline:
                    return _aborted(visible_redline)
                engine.press_key("return")
            else:
                engine.press_key(action.removeprefix("key:"))
        except Exception:
            return _fallback("UI engine action failed")

        history.append(action)

    return _fallback("step limit reached")
