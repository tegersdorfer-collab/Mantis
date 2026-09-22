# Jev UI Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative Jev-first controller to Mantis' existing macOS Accessibility `computer_task` flow so repeated closed-choice UI steps avoid a full qwen3.5:9b ReAct turn, while retaining the current qwen loop as the controlled fallback and writer.

**Architecture:** Keep `core.skills.uiauto._computer_task` as the public entry point and the existing low-level engine/safety boundary unchanged. Add a small Jev decision adapter in `core.decisions`, a pure/testable controller in `core/uiauto_controller.py`, and an integration branch in `core/skills/uiauto.py`. Each controller iteration snapshots the UI, builds a bounded closed `jevkit.Choice`, validates the returned action and current element again, performs at most one low-level action, and snapshots again. Any unavailable/uncertain/unsupported Jev path returns control to the existing qwen ReAct loop. `type_text` is the only action that invokes a one-shot qwen writer, with no tools.

**Tech Stack:** Python 3.14, existing `jevkit` decision layer, `tools.uiauto.engine`, `tools.uiauto.safety`, existing qwen `Agent`/`OllamaBackend`, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-22-jev-ui-controller-design.md`

## Global Constraints

- Work only in the isolated worktree `/Users/timoegersdorfer/Mantis/.worktrees/jev-ui-controller` on branch `feat/jev-ui-controller`; do not modify or clean the user's dirty main checkout.
- Do not read or modify any `.env` file, credentials, API keys, user data, or real application state. Unit tests must patch providers and engines; no test may click, type, send, buy, publish, delete, or alter a live macOS app.
- Preserve the public `computer_task(goal, app)` contract, the existing qwen ReAct fallback, `tools.uiauto.safety.is_redline`, permission checks, and existing UI tool behavior unless a test requires the smallest compatible seam.
- Never execute an element unless the controller has re-resolved the current ref, confirms it is enabled, and passes `safety.is_redline`. Invalid, stale, disabled, secure, or redline targets are controlled fallback/abort outcomes, never direct actions.
- Treat Accessibility titles and values as untrusted data. Jev may select only a controller-generated option; it must not receive or invent tool syntax, arbitrary refs, or unrestricted instructions.
- Keep Choice options below the jevkit maximum of 255. If the current snapshot cannot be represented safely within that limit, fall back to qwen without silently truncating options.
- Do not put complete UI text or typed input into the Jev cost log. Use the existing `JEV_LOG_PATH` decision log and technical metadata only; state hashing is the privacy boundary.
- Use the conservative setting default `JEV_UI_ACT_CONFIDENCE = 0.55`; do not lower it based on unit tests alone. Document that any real threshold tuning requires a separate replay/smoke measurement.
- Add tests before production implementation for every new behavior. Run focused tests after each task, then `python3.14 -m pytest tests/ -q` and `python3.14 -m ruff check .` before claiming completion.
- Each completed task must be committed on this branch with a focused message and a co-author trailer; do not push, merge, or create a pull request.

## Task 1: Add the Jev UI-action decision adapter and threshold setting

**Files:** `settings.py`, `core/decisions.py`, `tests/test_decisions.py` (extend existing tests if present), `tests/test_settings.py` if the repository has a settings test module.

- [ ] Add a failing test for a successful UI Choice decision: a patched Jev response with choice `click:ok` and confidence at least `config.JEV_UI_ACT_CONFIDENCE` returns an `Answer` whose value is `click:ok`.
- [ ] Add a failing test for a low-confidence response, provider unavailability, and malformed choice: each returns `None` without raising into the skill layer.
- [ ] Add a failing test that the serialized UI state is wrapped with `jevkit.untrusted`, and that decision logging receives only the hashed state/technical metadata rather than clear UI titles or values.
- [ ] Add `JEV_UI_ACT_CONFIDENCE = 0.55` beside the existing Jev settings with a short comment describing its conservative UI-action role.
- [ ] Implement `core.decisions.ui_action(state, criteria)` as the single adapter for this feature. It must build a `jevkit.Choice` whose instructions ask for exactly one safe next action, pass the bounded state as untrusted data, call the existing Jev `decide` path, and accept only the configured ACT band. Preserve a raw compatible Choice answer for the existing decision log where the logger supports it.
- [ ] Ensure all provider errors, disabled Jev, low confidence, and invalid result kinds become `None`, while ordinary existing decision helpers remain behaviorally unchanged.
- [ ] Run `python3.14 -m pytest tests/test_decisions.py tests/test_settings.py -q` (or the existing settings test path) and commit as `feat: add Jev UI action decision adapter`.

## Task 2: Define and test the pure controller state and closed action mapping

**Files:** create `core/uiauto_controller.py`, create `tests/test_uiauto_controller.py`.

- [ ] Add tests first for a bounded state builder with the exact fields `goal`, `app`, `step`, `elements`, and `history`; cap history to the last 8 entries and bound each UI string to a small fixed size before it reaches Jev.
- [ ] Add tests first for action construction: include `click:<ref>` only for enabled actionable elements; include `type_text` only for an enabled non-secure text field; include `key:return` when a non-secure text field exists; include `key:escape` when a snapshot has usable elements; always include `done` and `abort` for a representable snapshot.
- [ ] Add tests that secure fields and disabled elements are excluded from the choice mapping, and that a snapshot requiring more than 255 options reports a controlled `fallback` condition instead of truncating.
- [ ] Add tests for strict parsing: only the generated exact strings `click:<ref>`, `type_text`, `key:return`, `key:escape`, `done`, and `abort` are accepted; arbitrary refs and tool-like strings are rejected.
- [ ] Implement small typed data structures for controller results and action plans. Use `ControllerResult(status, text, reason="")` with statuses `completed`, `aborted`, and `fallback`; expose pure helpers `build_state`, `build_choice`, and `parse_action` so this logic can be tested without macOS.
- [ ] Keep element serialization data-only and bounded. Do not include raw Accessibility objects, passwords, complete user input, or unrestricted prompt text in the state sent to Jev.
- [ ] Run `python3.14 -m pytest tests/test_uiauto_controller.py -q` and commit as `feat: add bounded Jev UI action mapping`.

## Task 3: Implement the deterministic controller execution loop

**Files:** `core/uiauto_controller.py`, `tests/test_uiauto_controller.py`, with only the smallest test seam in `tools/uiauto/engine.py` if needed.

- [ ] Add tests first using fake snapshots, fake Jev decisions, fake engine actions, and a writer callback. Cover: one click per iteration; re-resolution of the ref before acting; `done` and `abort`; low confidence/invalid action/provider failure returning `fallback`; redline returning `aborted` without an engine action; stale/disabled refs returning `fallback`; `type_text` calling the writer exactly once and then using only the existing typing routine; engine exceptions returning `fallback`.
- [ ] Implement `run(goal, app, max_steps=UI_MAX_STEPS, writer=None)` as a synchronous, bounded loop. Each iteration must snapshot, build state/choice, call `core.decisions.ui_action`, parse the returned value, re-resolve and validate any target, perform at most one action, append a short action label to history, and continue until a terminal result or fallback.
- [ ] For `type_text`, call a writer callback with the current goal/state and require plain non-empty text. The controller must never let the writer select a click/key action or receive UI tools. Use the existing `engine.type_text` path only after validating the current non-secure text field; if no writer is supplied or writing fails, return `fallback`.
- [ ] Keep the existing safety module as the last click gate. A redline selection returns `ControllerResult("aborted", ...)` and does not call `engine.act`; no controller action may bypass `safety.is_redline`.
- [ ] Keep all controller output short and non-sensitive. Record fallback reasons through normal Python logging and let the Jev adapter handle the optional technical decision log; do not add clear snapshots to cost logs.
- [ ] Run `python3.14 -m pytest tests/test_uiauto_controller.py tests/test_uiauto_engine.py tests/test_uiauto_safety.py -q` and commit as `feat: execute safe Jev UI actions deterministically`.

## Task 4: Integrate Jev-first routing, qwen writer, and documentation

**Files:** `core/skills/uiauto.py`, `tests/test_uiauto_skill.py`, `README.md` or the closest existing UI-automation documentation.

- [ ] Add failing integration tests that patch the controller to return `completed`/`aborted` and assert the old qwen Agent path is not called, plus a `fallback` result that asserts the existing qwen path is still called. Preserve current permission-denial, redline, normal UI tool, and trusted-app tests.
- [ ] Add a test proving the writer path is a single no-tools qwen call with a small output budget and that it cannot access `ui_click`, `ui_type`, `ui_key`, or any other tool.
- [ ] In `_run_ui_agent`, call the Jev controller first after the existing trust/permission checks. Return terminal controller text directly; on `fallback`, log the reason and execute the current qwen ReAct code path with its existing tool allow-list and step limit. Do not duplicate the low-level safety logic in the skill wrapper.
- [ ] Implement the default writer in the skill layer (or a private controller helper) using the existing qwen backend configuration, a prompt that requests only field text, `use_tools=False`, no allowed tools, and a small output limit. Keep free-form planning and all non-text interaction in the old qwen fallback.
- [ ] Update concise documentation with activation through existing Jev settings, the `JEV_UI_ACT_CONFIDENCE=0.55` default, the closed-choice/safety boundary, and the fact that qwen remains the fallback/writer. Do not add keys or secrets.
- [ ] Run `python3.14 -m pytest tests/test_uiauto_skill.py tests/test_uiauto_controller.py tests/test_decisions.py -q` and commit as `feat: route computer task through Jev controller`.

## Task 5: Full verification and cost-evidence replay

**Files:** tests and documentation only if verification exposes a concrete defect; otherwise no production changes.

- [ ] Add or finish an offline replay test that counts qwen Agent calls for a deterministic multi-step closed-choice flow and compares Jev-controller routing with the old fallback route. The test must report counts, not claim a production savings percentage.
- [ ] Inspect the final diff for accidental secrets, `.env` access, clear UI/input logging, unbounded Choice options, safety bypasses, or changes to unrelated dirty work. Use `git diff --check` and `git status --short`.
- [ ] Run the focused regression set, then the complete suite with `python3.14 -m pytest tests/ -q`, then lint with `python3.14 -m ruff check .`. If a test or lint failure appears, fix it in the smallest scoped commit and rerun the covering command.
- [ ] Verify the final branch contains the design spec, this plan, all focused tests, and no changes outside the approved Jev UI-controller scope. Record exact pass counts and any limitations in the final report.
- [ ] Commit any verification-only fix as `test: verify Jev UI controller routing`; otherwise record the verification evidence without an empty commit.

## Completion criteria

- [ ] All five tasks are committed and reviewed in the isolated worktree.
- [ ] Jev can select only bounded, controller-generated UI actions and never bypasses the existing redline guard.
- [ ] Jev unavailability, uncertainty, malformed choices, unsupported snapshots, and writer failures cleanly return to the old qwen path.
- [ ] The public UI skill behavior and permission boundaries remain covered by regression tests.
- [ ] Full pytest and Ruff commands pass, with exact output captured in the task report.
- [ ] No push, merge, pull request, `.env` change, or live UI action was performed.
