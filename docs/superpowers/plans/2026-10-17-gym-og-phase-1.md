# openGym Read-only Phase 1 Implementation Plan

> **For agentic workers:** This task is implemented inline in the existing `feat/gym-og` worktree. Commits are not allowed.

**Goal:** Serve the openGym frontend at `/og/` behind the existing Gym session and expose a read-only snapshot of Mantis gym data.

**Architecture:** Keep the legacy Gym app at `/`; add a separately authenticated `/og/` static mount and `/og/api/*` endpoints. Map Postgres rows into openGym's state shape with a focused pure mapper, and track server revisions with an idempotent migration and database triggers. Build the fork with its bundled catalogue and optional capabilities disabled.

**Tech Stack:** FastAPI, PostgreSQL migrations in `core/db.py`, pytest, React 19, Vite 8, Zustand, Vitest.

**Spec:** `docs/specs/2026-10-17-gym-opengym-fork.md`

## Global Constraints

- Preserve the legacy Gym app under `/`.
- Do not commit.
- Never serve the openGym app without a valid `mantis_gym` session cookie.
- Phase 1 writes always return `{error: 'read_only'}` and local changes remain in the browser.
- Exercise data comes only from Mantis `exercises`; no catalogue exercise media is requested.
- Tests use synthetic data and `mantis_test`; the local database may be unavailable.

## Review Focus

- `/og/` and `/og/api/*` without a session must not expose app or data — API auth and static auth tests.
- A foreign Origin must be rejected for writes — read-only PUT origin test.
- Timed sets and warmup sets must preserve their meaning — synthetic mapping tests.
- Missing dist must not interfere with `/` — static delivery fallback test.
- A read-only response must not trigger repeated PUTs or discard local edits — Zustand test.

---

### Task 1: Snapshot mapping

**Files:** Create a focused mapper under `web/`; test it in `tests/test_gym_app.py` or a dedicated mapper test.

- [x] Write failing synthetic tests for workouts, plan routines, exercise metadata, time sets, warmups, volume, and empty history.
- [x] Run the targeted pytest tests and confirm expected failures.
- [x] Implement the pure mapping from supplied rows into the openGym state contract.
- [x] Re-run the targeted tests.

### Task 2: Authenticated API, revision migration, and smoke script

**Files:** Modify `web/gym_app.py` and `core/db.py`; create `scripts/gym_og_smoke.py`; extend Gym API tests.

- [x] Test `/og/` auth gate, `/og/api/data` auth, revision, disabled config, health, read-only PUT, unsupported routes, and Origin handling.
- [x] Run those tests and confirm expected failures.
- [x] Add the `gym_sync_state` table and revision triggers for gym-relevant tables; add authenticated `/og/api/*` routes.
- [x] Implement the CLI smoke script with an explicit database URL and required-state-field checks.
- [ ] Re-run the API/migration tests and exercise the smoke script when a test DB is available. The API/migration tests passed or skipped for unavailable PostgreSQL; the smoke script ran but localhost:5432 is blocked by the sandbox.

### Task 3: Frontend build and read-only behavior

**Files:** Create `scripts/build_gym_og.sh`; minimally patch the openGym frontend and add Vitest coverage.

- [x] Test empty built-in catalogue selection, disabled auxiliary capabilities/media, and one read-only rejection with no retry loop.
- [x] Run the targeted Vitest tests and confirm expected failures.
- [x] Add Mantis build flags or small `// mantis:` guards and show the requested read-only hint.
- [x] Run `npx vitest run` and the Vite build. The build passed; the full Vitest command has failures because this fork omits cross-tree API, Android, MCP, and catalogue files referenced by upstream tests.

### Task 4: `/og/` login return path and static files

**Files:** Modify `web/gym_app.py`; minimally patch the existing token login return behavior; test root preservation, missing dist, asset paths, service worker, and manifest scope.

- [x] Write failing tests for `/og/` auth redirects and path-scoped app files.
- [x] Run the targeted tests and confirm expected failures.
- [x] Serve built output only when present and redirect the legacy login flow back to `/og/` after a successful session.
- [x] Re-run targeted API tests, frontend tests, and build.

## Verification

- `python3.14 -m pytest tests/ -q`: 3,984 passed, 85 skipped.
- Targeted Vitest checks: 91 passed; full `npx vitest run`: 384 passed, 20 test files failed due missing files in the fork, plus 2 tests that inspect missing Android/MCP trees.
- `scripts/build_gym_og.sh`: passed. The Mantis exercise helper is 56,778 bytes; sample catalogue image and clip references are absent from the built assets.
- Ruff and `git diff --check`: passed.
- The smoke script could not reach the supplied `mantis_test` URL because the sandbox blocks `localhost:5432`.

