# Live smoke report (replaces the planned T20 security and T21 e2e pytest suites)

Date: 2026-11-16. Branch `build/one-shot`. Target: the real FastAPI app in Docker, real Postgres 16, exercised over HTTP.
No pytest suite was written; the full suite was not run (only `tests/core` after the one fix: 136 passed).

## What ran

| Piece | Path | Role |
|---|---|---|
| Launcher | `scripts/smoke_server.py` | The real app with the AI provider forced to a deterministic `MockProvider` (question generation, scoring, report-chat interpretations) and email captured in memory. No real AI call, no SMTP. |
| Shared helpers | `scripts/smoke_lib.py` | httpx client, PASS/FAIL reporting, RFC 6238 TOTP, small valid PNG, tenant bootstrap. |
| E2E | `scripts/smoke_e2e.py` | Full product flow, one PASS/FAIL line per step, exit 1 on any failure. |
| Security | `scripts/smoke_security.py` | Spec 11 checks, live. |

How it was run (own compose project, so the default dev DB was untouched):

```sh
# env file outside the repo: JWT keys, FIELD_ENCRYPTION_KEYS, TOKEN_PEPPER, DEFAULT_SA_*,
# RATE_LIMIT_LOGIN=200/minute, RATE_LIMIT_EXAM_LINK=200/minute (the scripts log in and open exam links
# far more often than the production defaults of 10/min and 20/min allow)
docker compose -p hrsmoke -f docker/docker-compose.yml up -d db test-db
docker compose -p hrsmoke ... run --rm --no-deps api alembic upgrade head
docker compose -p hrsmoke ... run --rm --no-deps api python -m app.cli create-default-superadmin
docker compose -p hrsmoke ... run -d --name hrsmoke-api --no-deps -p 8001:8000 api python scripts/smoke_server.py
docker compose -p hrsmoke ... run --rm --no-deps api python scripts/smoke_e2e.py --base-url http://hrsmoke-api:8000
docker compose -p hrsmoke ... run --rm --no-deps api python scripts/smoke_security.py --base-url http://hrsmoke-api:8000
```

The scripts need `httpx` (and `asyncpg` for the database checks), both in the api image, so run them in the container. Host default base URL is `http://localhost:8001`. The scripts create uniquely named organizations per run, so they can be repeated on the same database. The security script uses `DATABASE_URL`/`SMOKE_DATABASE_URL` for the expiry, deadline, audit append-only and RLS checks, and calls `python -m app.cli run-job timeout_attempts` for the timer sweeper; those steps are skipped without a database URL.

## Result (final run, after the fix below)

| Script | Passed | Failed | Skipped |
|---|---|---|---|
| `smoke_e2e.py` | 105 | 0 | 0 |
| `smoke_security.py` | 118 | 0 | 0 |

### E2E coverage
SA login, default-SA flag, org plus org admin creation (one-time temporary password), forced password change (other routes blocked until done, weak password rejected), OA TOTP setup/verify/login challenge/wrong code, HR creation and forced password change, AI settings (key write-only), consent document create/version/publish, assessment with all five question types, duration/pass mark/instructions, preview hides keys, publish and immutability, AI question suggestions (mock), role create/open/link, invite (exam URL once, duplicate refused, raw token never listed), candidate session open, state, consent version, device check (narrow viewport refused), baseline photo (non-image refused), start (server deadline, idempotent), questions without keys, autosave for each type, heartbeat, proctoring events (client severity ignored), snapshot, submit (idempotent, locked afterwards), auto scoring, mock-AI rescore, full attempt report, score override, signed snapshot URL, decision, second decision refused, OA override, decision history, help-request branch (start blocked, HR approves with extra minutes, extended deadline), audit log actions and CSV export (the export is itself audited), SA cross-org audit export, all 14/12/6 dashboard gadgets for OA/HR/SA, layout, notifications, and M11 reports (catalog, interpret via mock AI, select, preview-count, generate CSV, download with decrypted name, history, audit, HR refused, SA structured `filter_spec` path and download without candidate content).

### Security coverage
- Tenant isolation: org A OA and HR against org B ids on 68 read/update/delete routes covering assessments, versions, questions, question bank, roles, role links, invites, help requests, attempts, snapshots, team users, documents, audit rows and notifications (all 404 for OA; 403/404 for HR); list endpoints never contain org B ids; org B data unchanged afterwards; candidate of org A cannot answer an org B question; RLS fails closed for `app_rw` without a tenant.
- Role guards: `x-roles` walk of `/openapi.json`, every operation called with every role not in its `x-roles` (SA, OA, HR, candidate, anonymous): all 401/403.
- Owner rules (spec 4.3): non-owner HR sees name/status/decision only, cannot open attempts or snapshots, decide, edit, invite, link or reassign; OA cannot make the owner-only first decision; SA cannot read attempts.
- Last-admin and default SA: last admin protected (SA path, OA path, creating admins), second admin lets the first be deactivated and its token dies, default SA cannot be deactivated/reactivated/reset/unlocked or have its password changed.
- Exam clock: spoofed `Date`/client-time headers and event times ignored, server-side remaining time, HR extra time, deadline plus grace refused with `EXAM_TIME_OVER`, timer sweeper auto-submits with `submit_reason=timer` (badge shown).
- Invite links: binding (other browser refused, reset-binding cuts the old session), regeneration (old token dead), revoke (410), expiry (410) and extend, unknown/malformed tokens, raw token and candidate email not stored in plain text, link carries no personal data, extend refuses a past date.
- Feature flags: off flags give 403 `FEATURE_DISABLED` with the flag name, SA not gated, `/auth/me` map, running exams not interrupted, `camera_proctoring` read at the next start, dependency cascade, flag changes SA-only.
- Audit append-only: no mutating audit routes; UPDATE/DELETE rejected for both `app_rw` and the table owner (trigger), TRUNCATE rejected for `app_rw`.
- Auth hardening: generic login error, 5-failure lockout and OA unlock, refresh cookie HttpOnly, rotation, reuse refused and family revoked, sign-out-everywhere kills access tokens, garbage token 401, CSV formula injection neutralised, document HTML sanitised, upload size and magic-byte checks, snapshot interval limit, 50-event cap, exam-link rate limit (429), CORS unknown origin, `X-Request-ID`, security headers, error envelope.

## Bugs found and fixed

| # | Finding | Owner | Fix | Commit |
|---|---|---|---|---|
| 1 | Spec 10 requires security headers (HSTS, content-type options, frame options); responses carried only `X-Request-ID`. | `app/core` (needed: no module owns responses) | New `SecurityHeadersMiddleware` in `app/core/middleware.py` (nosniff, DENY, no-referrer, HSTS) registered in `app/main.py`. `tests/core` re-run: 136 passed. | `6f5dc55` |

All other first-run failures were wrong expectations in the new scripts (409 vs 422, 410 vs 403 status choices, help-request ordering, refresh cookie name, per-IP rate limits of the smoke environment) and were corrected in the scripts, not in the application.

## Observations and residual risks (no code change made)
- Regenerating a link keeps the browser binding, so a regenerated link opened from a new browser still needs `reset-binding`. Consistent with the spec text, but HR may expect regeneration to also release it.
- Start checks run in the order consent, device check, camera, help request: a candidate with no camera hits `CAMERA_REQUIRED` before `HELP_REQUEST_PENDING`; the route to start is still the help request approval with camera exemption.
- Access tokens stay valid up to 15 minutes after plain logout (already documented); sign-out-everywhere, deactivation and password change are immediate (verified).
- Login rate limit (10/min) and the lockout/rate interplay were not stress tested: the smoke environment raises `RATE_LIMIT_LOGIN` and `RATE_LIMIT_EXAM_LINK`. The exam-link limit itself was verified to return 429.
- Not covered live: Google sign-in, SMTP send, S3 storage driver, PDF export of reports and audit, bulk CSV invite, org disable/enable, retention purge and the other background jobs (only `timeout_attempts` ran), data erase, branding upload and contrast checks. These remain covered only by the per-module tests.
- The security script moves invite expiry and attempt deadlines with direct SQL as the database owner (the only way to test expiry without waiting); that is a harness shortcut, not an API path.
