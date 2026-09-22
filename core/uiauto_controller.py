"""Pure state and closed-choice helpers for the Jev UI controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import re
from typing import Iterable, Literal, Mapping

from tools.uiauto.engine import ACTIONABLE_ROLES
from tools.uiauto.safety import is_secure_field


MAX_HISTORY = 8
MAX_STRING_LENGTH = 160
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea"})
MAX_CHOICE_OPTIONS = 255


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
