# Build state and resume guide (tracked checkpoint)

Branch `build/one-shot`. Plan: `docs/superpowers/plans/2026-11-16-backend-one-shot-build.md`. Spec: `docs/spec/MASTER-SPEC.md`.
Controller ledger (git-ignored, richer): `.superpowers/sdd/2026-11-16-backend-one-shot-build/progress.md`.
Run anything inside Docker: `docker compose -f docker/docker-compose.yml run --rm api pytest tests/<module> -q`.

## Done and committed
Task 1 skeleton, Task 2 core, Task 3 schema/RLS (+ runtime `SET LOCAL ROLE` fix), Task 4 auth (M1, 14 tests), Task 9 audit (M10, 58 tests).

## In progress (stopped by the user at ~13:25 IST to continue in the CLI; code on disk, WIP-committed)
T5 m02_platform + m03_team (m02 mostly missing), T6 m04_org_settings, T7 m05_assessments (+T8 m05b, not started), T10 m14_ai (no tests yet). Each needs its lean tests green and a real commit with the message from its brief.

## How to resume in the CLI
1. `cd hr-assessment-backend && git checkout build/one-shot`; read this file and the ledger.
2. Task briefs are in `.superpowers/sdd/2026-11-16-backend-one-shot-build/task-N-brief.md` (regenerate with the superpowers `subagent-driven-development` `scripts/task-brief` if missing). Common rules: `module-agent-common.md` in the same folder.
3. Resume order (critical path first): T7+T8, T5, T6, T10, then the Wave 1 gate fixes, then Wave 2.
4. Agents from the old session cannot be resumed from a new CLI session; dispatch fresh agents from the briefs and tell them the module dirs already contain WIP to continue from.

## Pending
Wave 2 (T11 roles, T12 invites, T13 reports, T14 dashboard), T15 exam, T16 review, T17 jobs, T18 gadget data, T19 AI scoring, T20 security suite, T21 e2e, T22 seed, T23 openapi, T24 handover.

## Gate fixes owed at the Wave 1 gate
See ledger: migration hardening (reports_ro column grants, RLS on password_history/refresh_tokens, ALTER ROLE password hygiene); route prefix check (`/api/v1` in routers); autouse engine-dispose fixture.

## Budget policy (user instruction)
- Ledger/checkpoint after every task completion.
- ~80% of usage: only critical-path tasks (T7 -> T11/T12 -> T15 -> T16), no new parallel agents.
- ~90% or any HTTP 429: stop dispatching, commit WIP, update this file, wait for the reset, then resume with SendMessage to the same agents (or re-dispatch from the brief files).
- Lean tests: 8-12 per module (see plan Global Constraints).

## Serial-mode checkpoints (single agent, lean tests; supersedes the parallel resume steps above)
- [x] T7 m05 assessments green (98 passed). [x] T8 m05b question bank (7 tests, commit `feat(m05b)`).
- [x] T5 m02+m03 (61 tests). [x] T6 m04 (router written, 207 tests). [x] T10 m14 (7 tests, router prefix fixed to /api/v1).
- Security-review fixes applied: M4 SMTP host DNS guard in prod (`netguard.py`); password omitted = keep (no plaintext compare).
- Decision to confirm: AI `base_url` (openai-compatible) is still not DNS-restricted in prod (self-hosted LLMs); SMTP is.
- [x] Wave 1 gate fixes: reports_ro column grants (users, invites), RLS on password_history/refresh_tokens (child of users), ALTER ROLE charset check + log_statement none, all 118 operations under /api/v1, autouse engine fixture. Full suite: 587 passed. Dev DB must be recreated (baseline migration edited).
- Handover notes: access tokens live <=15 min after logout; JWT single kid; uvicorn --proxy-headers; SMTP DNS re-resolve only at save/test (rebinding residual); export caps 50000 CSV / 2000 PDF rows.
- [x] T11 M6 roles (14 tests). Deviation: `hooks.on_role_close` hooks take `(ctx, session, role_id)` so M7's revoke is in the same transaction. `RoleService.get_open_linked_role(session, org_id, role_id) -> (OpenRole, version_id)`.
- [x] T12 M7 invites + help requests (14 tests). Decisions: `send-email` rotates the token and emails the new link (raw token is never stored); M7 registers `revoke_unstarted` on `on_role_close`; M4 gained `EmailSettingsService.smtp_for_sending`.
- [x] T15 M8 exam engine (14 tests; RF-2 race and grace, RF-3 payloads). Interfaces: `ExamService.finalize(ctx, session, attempt_id, reason)` (idempotent, row-locked; T17 sweeper calls it with reason `timer`), `hooks.on_submit` (T16 registers scoring), `attempt_remaining_seconds`, `severity_for`. `attempts.flag_counts = {"types": {type: n}, "severity": {sev: n}}`. Answer shapes: `{option_id}`, `{option_ids}`, `{blanks}`, `{gaps}`, `{text}`. M4 gained `DocumentService.published_view`.
- Security-review fixes to M8: attempt row lock on answer save, no snapshot re-attach, binding checked against the invite's stored hash on every request (reset-binding cuts off the old session), revoked invites lose access. Acknowledged: org-disabled is checked at open only (per-request query cost).
- [x] T16 M9 review (10 tests). Scoring runs from `exam_hooks.on_submit`; `ScoringService.score_attempt/recompute`; `hooks.ai_scoring_hooks` for T19; `ReviewService.erase_candidate_data` for T17. The worker/jobs process must import `app.modules.m09_review.scoring` so the submit hook is registered when the sweeper finalizes attempts.
- Full suite: 641 passed, lint and name check clean (after T16).
- Next (critical path done through M9): T13 M11 reports, T14 M12 dashboard, T17 jobs, T18 gadgets, T19 AI scoring, T20 security suite, T21 e2e, T22 seed, T23 openapi, T24 handover.
