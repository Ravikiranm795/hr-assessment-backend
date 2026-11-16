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

## Round 2: `scripts/smoke_extra.py` (the "not covered live" list)

Date: 2026-11-17. Own compose project `-p hrsmoke2` (port 8002), same launcher. Three api containers from the
same image: the main one (mock AI, captured email), one with `SMOKE_REAL_SMTP=1` (real `aiosmtplib` delivery to a
capture SMTP server the script starts), one with `STORAGE_DRIVER=s3` pointing at a closed port. Run inside the api
container: `python scripts/smoke_extra.py --base-url http://hrsmoke2-api:8000 --smtp-url http://hrsmoke2-api-smtp:8000 --s3-url http://hrsmoke2-api-s3:8000`.

| Script | Passed | Failed | Skipped |
|---|---|---|---|
| `smoke_extra.py` | 230 | 0 | 4 (need real credentials: Google ID token, real SMTP relay/TLS, real S3 bucket; no candidate-report PDF endpoint exists) |

### Covered
1. **PDF exports**: audit PDF (OA and SA) and M11 report PDF are structurally valid PDFs (header, `%%EOF`, pages, every Flate stream decodes; `smoke_lib.pdf_text` decodes page text through the ToUnicode maps); document title and page header carry the application name; rows carry real actions / decrypted candidate names; column choice and action filter honoured; unknown column 422; HR refused; the export is audited with format and row count; HTML in cell values printed literally. Watermark: candidate `state` carries it, SA can switch it off/on, OA cannot change the flag. No candidate-report PDF endpoint exists (spec M9 is JSON), marked skipped.
2. **Bulk CSV invites**: BOM, quoted names with commas, per-row codes (`email_invalid`, `name_invalid`, `phone_invalid`, `formula_not_allowed` for leading `=`/`+`/`@` in name and `=` in email, `duplicate_in_file` incl. case/whitespace variants, `DUPLICATE_INVITE` against existing invites), created rows return a one-time exam URL that opens a session, totals add up, re-upload creates nothing, `INVITE_BULK_CREATED` audited, emails not stored in plain text; whole-file refusals: empty, bad/extra header, non UTF-8, missing field, 201 rows (200 accepted), over 512 KiB; flag `csv_bulk_invite` off gives `FEATURE_DISABLED` while single invites still work; closed role refused; OA allowed, anonymous 401.
3. **Org disable/enable**: HR/OA login `ORG_DISABLED`, refresh refused, pre-issued access tokens refused, unopened link refused, a session opened before the disable cannot start (fixed, see below), an in-progress attempt keeps saving, heartbeating and submits; SA still reads it, disabled filter, `ORG_DISABLED`/`ORG_ENABLED` audited, double disable/enable 409; after enable everything works again.
4. **Candidate data erase**: before/after verified in the DB: `attempt_answers` and `proctor_events` rows gone, phone NULL, `pii_purged_at` set, baseline reference, ip and user agent cleared, snapshot/baseline asset rows deleted, the old signed snapshot URL stops serving; summary kept (score, decision, flag counts); default keeps name/email, `include_identity=true` replaces the name and removes email+phone and scrubs the blind index (re-invite with the same email works); audit `CANDIDATE_DATA_DELETED` kept with counts and no PII, repeatable, both audit rows survive; HR 403, foreign OA 404, anonymous 401.
5. **Branding upload / signed URLs**: valid PNG accepted, served inline with nosniff/no-store/sandbox CSP; tampered, swapped, unsigned, garbage, traversal and forged tokens all 404; replaced logo's file deleted; 60 s expiry enforced (checked at the end of the run); refused: SVG, type/content mismatch, random bytes, HTML, truncated PNG, 8 px, 9000 px header, PDF, GIF, ICO-as-logo, over 5 MB, empty, missing file; minimal JPEG favicon accepted; HR 403, anonymous 401; contrast validate/save (`BRANDING_CONTRAST_TOO_LOW`), bad hex, non-curated font; another org's asset id cannot be referenced, themes isolated; reset keeps the logo; `BRANDING_UPDATED` audited; candidate state exposes a working signed logo URL; `org_branding` flag.
6. **Background jobs** via `python -m app.cli run-job`, all 9 in `list-jobs` (incl. the new `auth_cleanup`), each run twice: `expire_invites` (expires overdue, 410 afterwards, one `invite_expiring` warning, idempotent), `timeout_attempts` (auto-submit `timer`, scored, late answer refused, oversized snapshot refused), `retry_ai_scoring` (unreachable provider: `needs_manual` + owner notified, job exits 0; with the mock provider in-process: completes), `retention_purge` (old decided purged, recent and undecided untouched, `RETENTION_PURGE_RUN`, idempotent), `notification_cleanup` (read > 60 d removed, unread kept), `report_cleanup` (file gone after 7 d, history row stays), `auth_cleanup` (expired refresh tokens removed, live kept), `version_purge` (clean, in-use versions untouched), `audit_partitions` (drops a partition older than 13 months, keeps the 13-month horizon, `AUDIT_PURGE_RUN`, idempotent, no audit row older than 13 months).
7. **Google / SMTP / S3 (offline)**: Google sign-in with garbage or `alg=none` token is a clean 401, allowed-domain validation; settings validation in a clean process (prod without secrets, unknown driver/env, `STORAGE_DRIVER=s3` without bucket now refused, local starts). SMTP: `SMTP_NOT_CONFIGURED` before setup, password write-only and decrypted correctly before use (the capture server saw the real credentials), CRLF/port/security/sender validation, test endpoint delivers and sets `verified_at`, invite `send_email` delivers a working link, `send-email` rotates the token (old link dead), audited, unreachable host / STARTTLS-not-offered / implicit TLS on a plain server all give `sent=false` + reason (no 500), disabled settings refuse, HR 403. S3 against a closed port: 500 with the error envelope, no stack trace or endpoint in the body, no dangling logo reference.
8. **Open questions from round 1** (decided against the spec, documented, no code change):
   - (a) Regenerating a link keeps the browser binding: spec M7 says `regenerate-link` "rotates the token (old link dies)" and `reset-binding` is the only action that "releases the browser-session binding with a reason"; spec 8.1 binds the *invite* ("the invite works only from that browser"), not the token. Current behaviour is per spec: regenerate does not release the binding. Verified: new link refused from a new browser (`LINK_BOUND_TO_OTHER_SESSION`), resumes from the same browser, opens anywhere after `reset-binding`. HR UI should offer reset-binding next to regenerate.
   - (b) `CAMERA_REQUIRED` before `HELP_REQUEST_PENDING`: spec 8.2 lists camera first ("Without a camera permission report and a baseline photo, `start` fails with `CAMERA_REQUIRED`. `start` also requires consent and no open help request"); no ordering is mandated and both block. Kept as is. Verified the realistic path: a no-camera browser is stopped at the system check itself (`CAMERA_REQUIRED`), raises a help request, HR approves with `camera_exempt`, the candidate then passes the check and starts.

### Bugs found and fixed (round 2)

| # | Finding | Owner | Fix | Commit |
|---|---|---|---|---|
| 2 | A candidate who opened the link before the organization was disabled could still `start` the exam (spec 5: a disabled org has no new exam starts; running attempts finish). | `m08_exam` | `ExamService.start` checks the organization status after the idempotent in-progress return. New test in `tests/m08_exam`: 17 passed. | `f2f6698` |
| 3 | `STORAGE_DRIVER=s3` with an empty `S3_BUCKET` started and failed on the first upload. | `app/core/config.py` (landed by the hardening agent alongside its config work) | Settings validator refuses s3 without a bucket; verified by the clean-process config check. | in `217dd84`/`3abd317` series |

Script-side fixes only (not bugs): audit export filter key is `action` (not `actions`) and the column key is `created_at`; the retry job's CLI process has no mock provider, so the unreachable-provider path is the real one and the mock path runs the same job function in-process; the PDF helper needs the exact `/Length` of each stream.

### Still not covered live
Real Google ID-token verification, a real SMTP relay (TLS/AUTH), a real S3 bucket, AI scoring against a real provider. The scripts mark these as skipped rather than failing.
