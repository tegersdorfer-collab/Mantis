# Fast-Path Merge Implementation Plan

> **For agentic workers:** Inline execution in this session. Do not commit, delegate, or review.

**Goal:** Merge Agent-Remote, Merk-Inbox, and Gym fast paths with one turn start, safe confirmation expiry, and feature-correct persistence.

**Architecture:** MessageHandler owns turn initialization and fast-path ordering. Orchestrator injects AgentRemoteService into it. Discord routes retain bounded image forwarding, safe mention handling, and every sent preview message ID.

**Tech Stack:** Python 3.14, pytest, Ruff, Discord adapter, AgentRemoteService.

**Spec:** User request in the active session; feature contracts in `docs/specs/2026-10-14-gym-coach.md`, `docs/specs/2026-10-14-merk-inbox.md`, and `tests/test_agent_remote.py`.

## Global Constraints

- Every incoming user turn calls `_begin_user_turn` before routing.
- A non-confirmation turn expires any pending Agent-Remote proposal; Inbox deletion confirmations keep their existing channel/reply/TTL binding.
- Priority: Inbox deletion confirmation and command → Agent-Remote confirmation and commands → Inbox management → clarification/accountability → Gym → Inbox capture → Agent.
- Agent-Remote is recognized only for an allowed user in a Discord DM.
- Agent-Remote history uses `use_case=agent` and marks assistant output untrusted; Gym and Inbox retain their own KZG/LZG rules.
- Discord keeps both validated image attachments and every preview message ID, with mentions disabled for sent content.
- Run the requested pytest and Ruff commands with a hard time limit; do not commit.

## Review Focus

- A Gym turn between preview and confirmation must expire the preview — shared integration test asserts no process starts for the old ID.
- An Inbox capture after Gym must still route to Inbox with one persisted user row — shared integration test checks the use case and count.
- Stale or untrusted Discord confirmation must never authorize a process — remote service and combined-path tests check this.
- Long Discord previews must retain all reply-bindable message IDs — Discord route contract test checks returned IDs.
- Image messages must still forward validated bytes — existing Discord attachment test remains in the suite.

---

### Task 1: Pin the combined routing and persistence contract

**Files:**
- Test: `tests/test_gym_inbox_handler_integration.py`
- Test: `tests/test_agent_remote.py`

**Interfaces:**
- Consumes: `MessageHandler.handle`, `AgentRemoteService.handle`, `DiscordRoute.send`.
- Produces: A single integration scenario that creates a preview, sends a Gym turn, captures a YouTube link, then attempts the stale confirmation.

- [x] Write a failing test that asserts Agent-Remote preview persistence is tagged `agent` and untrusted, the intervening Gym turn is tagged `gym`, the YouTube turn is tagged `inbox`, and the stale ID starts no process.
- [x] Run the focused test; it failed because the `codex:` path skipped `_begin_user_turn`, then passed after routing moved into MessageHandler.

### Task 2: Merge turn initialization and fast-path priority

**Files:**
- Modify: `core/message_handler.py`
- Modify: `orchestrator.py`
- Modify: `domains/agent_remote/service.py`
- Test: `tests/test_agent_remote.py`

**Interfaces:**
- Consumes: `AgentRemoteService.begin_user_turn(msg)` for retaining only an eligible active confirmation; `MessageHandler.agent_remote` for dispatch after Inbox deletion handling.
- Produces: `MessageHandler.handle` calls `_begin_user_turn` first and then processes the required ordering without duplicate persistence.

- [x] Add integration assertions that an unrelated Gym/Inbox user turn clears a pending preview and an allowed `ja <id>` turn remains confirmable.
- [x] Implement the lifecycle method on AgentRemoteService, inject it into MessageHandler from Orchestrator, and move remote dispatch behind Inbox deletion handling.
- [x] Move Inbox management ahead of clarification/accountability while keeping Gym ahead of Inbox capture.
- [x] Merge `_persist_msg` semantics: skip LZG when requested, filter unsafe messages from KZG, retain metadata, and store each chat row once.
- [x] Run the focused integration and Agent-Remote tests; 210 passed.

### Task 3: Preserve Discord attachments and remote reply binding

**Files:**
- Modify: `communication/discord.py`
- Modify: `core/message_handler.py`
- Test: `tests/test_discord_channel.py`, `tests/test_agent_remote.py`

**Interfaces:**
- Consumes: `IncomingAttachment`, `_DiscordRoute.send`, `AgentRemoteService` preview ID normalization.
- Produces: All sent chunk IDs are returned; Inbox extracts its single prompt ID; image and safe-mention behavior remain intact.

- [x] Extend the Discord route contract test to send a multi-chunk preview and assert all chunk IDs and disabled mentions.
- [x] Merge the route return behavior: one ID for one message, all IDs for split previews; keep bounded image forwarding.
- [x] Run the focused Discord tests; 210 related tests passed.

### Task 4: Run requested verification

**Files:** No additional files.

- [x] Run `perl -e 'alarm 150; exec @ARGV' python3.14 -m pytest tests/ -q`: 2961 passed, 46 skipped.
- [x] Run `perl -e 'alarm 150; exec @ARGV' python3.14 -m ruff check .`: all checks passed.
- [ ] Confirm there are no conflict markers, unresolved paths, or commits. Conflict markers are gone; the two files still show `UU` because this sandbox exposes `.git` read-only and cannot stage the resolved files.
