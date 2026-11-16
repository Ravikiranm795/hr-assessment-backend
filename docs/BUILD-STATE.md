# Build state and resume guide (tracked checkpoint)

Branch `build/one-shot`. Plan: `docs/superpowers/plans/2026-11-16-backend-one-shot-build.md`. Spec: `docs/spec/MASTER-SPEC.md`.
Controller ledger (git-ignored, richer): `.superpowers/sdd/2026-11-16-backend-one-shot-build/progress.md`.
Run anything inside Docker: `docker compose -f docker/docker-compose.yml run --rm api pytest tests/<module> -q`.

## Done and committed
Task 1 skeleton, Task 2 core, Task 3 schema/RLS (+ runtime `SET LOCAL ROLE` fix), Task 9 audit (M10).

## In progress (code on disk, agents died on the 5-hour limit)
T4 m01_auth, T5 m02_platform + m03_team, T6 m04_org_settings, T7 m05_assessments (+T8 m05b), T10 m14_ai. WIP-checkpointed in a commit; each needs its tests green and a real commit.

## Pending
Wave 2 (T11 roles, T12 invites, T13 reports, T14 dashboard), T15 exam, T16 review, T17 jobs, T18 gadget data, T19 AI scoring, T20 security suite, T21 e2e, T22 seed, T23 openapi, T24 handover.

## Gate fixes owed at the Wave 1 gate
See ledger: migration hardening (reports_ro column grants, RLS on password_history/refresh_tokens, ALTER ROLE password hygiene); route prefix check (`/api/v1` in routers); autouse engine-dispose fixture.

## Budget policy (user instruction)
- Ledger/checkpoint after every task completion.
- ~80% of usage: only critical-path tasks (T7 -> T11/T12 -> T15 -> T16), no new parallel agents.
- ~90% or any HTTP 429: stop dispatching, commit WIP, update this file, wait for the reset, then resume with SendMessage to the same agents (or re-dispatch from the brief files).
- Lean tests: 8-12 per module (see plan Global Constraints).
