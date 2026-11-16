# Test report

Honest status of automated and live testing at the end of the one-shot build (branch `build/one-shot`). Companion to section 11 of `docs/HANDOVER.md`.

## Policy that shaped this report

The user's instruction during the build was **development first**: finish the product, keep tests **lean per module** (roughly 8 to 12 per module in the plan; some modules grew larger because of table-driven permission matrices), run **only the module being built**, and defer test writing and analysis to the end. Two planned suites were **replaced by live verification** against the real application and database:

| Planned | Replaced by | Result |
|---|---|---|
| T20 security suite (`tests/security`) | `scripts/smoke_security.py` | 118 checks passed, 0 failed |
| T21 end-to-end scenario | `scripts/smoke_e2e.py` | 105 steps passed, 0 failed |

Details, how to run them, the one bug they found and fixed (missing security headers, commit `6f5dc55`) and the observations are in **`docs/smoke_report.md`** (present, dated 2026-11-16). The scripts need a running server with a deterministic mock AI provider and captured email (`scripts/smoke_server.py`), the real Postgres, and raised login and exam-link rate limits; they are not part of `pytest`.

## What was run for this document

- `pytest --collect-only -q` once (collection only, about 6 seconds): **685 test cases collected** across 17 suites. **The full suite was not run** for this document (policy). Nothing here is a claim that all 685 pass today.
- `ruff check .` and `ruff format --check .`: clean (258 files). `sh scripts/check_name.sh`: ok.
- Rename check (`APP_NAME=Foo`, `APP_VENDOR_NAME=Bar`) in a throwaway container: passed, recorded in `docs/HANDOVER.md` Appendix A.
- A dump and restore of the development database into a second database: passed (48 public tables including audit partitions, revision `0002`).

## Last recorded results (from the build ledger)

| When | Result |
|---|---|
| After Wave 1 gate | full suite 587 passed |
| After the exam and review tasks | full suite 641 passed |
| After the jobs task | full suite 651 passed |
| After the AI scoring task | 663 passed and 2 failures that were then fixed (test isolation for the new hook); **the fix was not re-verified by a full run** |
| After the security-header fix | `tests/core`: 136 passed |
| Per-module, at each module's completion | every module's own suite was green when it was committed (for example M5 98, M10 58, M4 207) |

**A final full-suite run is still owed.** Run `docker compose -f docker/docker-compose.yml run --rm api pytest tests -q` (a few minutes, uses the disposable `test-db`) and record the result here.

## Collected tests per module

Counts are from `pytest --collect-only -q` (parametrized cases count individually, so cases exceed functions, notably M4). "Operations covered" is the number of OpenAPI operations the module owns.

| Suite | Test functions | Collected cases | Operations covered |
|---|---|---|---|
| `tests/core` core (schema, RLS, triggers, security, errors, storage, export, deps) | 106 | 136 | 4 |
| `tests/jobs` jobs (8 jobs) | 10 | 10 | - |
| `tests/m01_auth` M1 auth | 12 | 14 | 11 |
| `tests/m02_platform` M2 platform | 20 | 45 | 26 |
| `tests/m03_team` M3 team | 8 | 16 | 9 |
| `tests/m04_org_settings` M4 org settings | 74 | 207 | 30 |
| `tests/m05_assessments` M5 assessments | 16 | 98 | 19 |
| `tests/m05b_question_bank` M5b question bank | 4 | 7 | 6 |
| `tests/m06_roles` M6 roles | 5 | 14 | 13 |
| `tests/m07_invites` M7 invites and help requests | 9 | 14 | 13 |
| `tests/m08_exam` M8 exam engine | 9 | 16 | 14 |
| `tests/m09_review` M9 review | 6 | 10 | 11 |
| `tests/m10_audit` M10 audit | 20 | 58 | 11 |
| `tests/m11_reports` M11 reports | 10 | 16 | 14 |
| `tests/m12_dashboard` M12 dashboard, gadgets, notifications | 8 | 8 | 8 |
| `tests/m14_ai` M14 AI | 10 | 14 | 2 |
| `tests/seed` seed | 2 | 2 | - |
| **Total** | **329** | **685** | 191 |

Each module suite follows the plan: golden-path scenarios on mock data, a table-driven permission check against the matrix, and key negative cases, with a unique test database created from the Alembic migrations per session and a mock AI provider (no real AI call is ever made). The shared harness is `tests/conftest.py` (unique `test_<hex>` database, `migrated_db`, per-module truncate, autouse engine disposal) and `tests/factories.py`.

Run one module: `docker compose -f docker/docker-compose.yml run --rm api pytest tests/<module> -q`.

## Covered by live scripts (not by pytest)

Full product flow (SA to org to HR to assessment to invite to exam to review to decision to audit to report); tenant isolation on 68 read/update/delete routes across two organizations; every operation called with every role outside its `x-roles`; owner rules; last-admin and default super admin protection; server-side exam clock including the timer sweeper; invite link binding, regeneration, revoke, expiry; feature-flag blocking and cascade; audit append-only (UPDATE, DELETE, TRUNCATE rejected); lockout, refresh rotation and reuse, sign-out-everywhere; CSV injection, upload checks, security headers, CORS, request id, error envelope.

## NOT covered by automated tests: follow-up backlog

Ordered roughly by value.

1. **Final full-suite run** and a recorded result (owed, see above). Re-check the two failures fixed after the AI scoring task.
2. **Turn the live scripts into repeatable checks.** They are not in `pytest` and not in any CI: wire `smoke_e2e.py` and `smoke_security.py` into a CI job (compose project, seeded environment) or port them into `tests/security` and `tests/e2e` as the spec 11 describes.
3. **Not exercised live, covered only by per-module tests** (per `docs/smoke_report.md`): Google sign-in (verifier is injected), SMTP sending (memory sender), the S3 storage driver, PDF export of reports and audit through the API, bulk CSV invite, organization disable and enable, the retention purge and every job except `timeout_attempts`, candidate data erase, branding upload and contrast checks.
4. **Real external systems.** All AI paths use `MockProvider`: no contract test against Anthropic, Gemini, OpenAI, an OpenAI-compatible server or the CLI provider, and no prompt-injection regression set for generation, scoring and report chat. No real SMTP server, no MinIO or S3, no real Google token.
5. **Concurrency and load.** Only the exam submit/timer race (RF-2) and a few row-lock cases are tested. Missing: parallel candidates, answer save versus submit, snapshot upload throughput, login and lockout interplay under load (the smoke environment raises the login and exam-link limits, so only the exam-link 429 was verified), report engine with large row counts and the 15-second statement timeout.
6. **Operations.** Backup and restore were checked once by hand; there is no automated restore test. Untested: the least-privilege database topology (separate role logins, entrypoint migration with a non-owner URL), uvicorn behind a proxy with `--proxy-headers` and the rate-limit bucket behaviour, two workers at once, JWT, field-key and pepper rotation effects on live sessions, upgrading an existing database across a future migration, the `log_statement` behaviour of the role-password statement.
7. **Thin suites relative to risk.** M8 exam engine (9 test functions), M9 review and decisions (6), M6 roles (5), M5b question bank (4), seed (2). The decision and override matrix, version purge edge cases, help-request and accommodation interplay, and proctoring severity rules deserve more direct tests.
8. **Frontend and browser reality.** Camera, face-detection events, device and user-agent matrix, signed-URL display, CORS and cookie behaviour across sites and subdomains (refresh cookie is `SameSite=Strict`) are untested here; they need the frontend and real browsers.
9. **Security work beyond the smoke checks.** No dependency audit (`pip-audit`), no static analysis beyond ruff, no penetration test. Known and unmitigated items are listed as R1 to R16 in `docs/HANDOVER.md` section 10.3 (for example TOTP replay, DNS rebinding for SMTP, the unrestricted AI `base_url`, access tokens living up to 15 minutes after logout).
10. **Contract tests.** Response examples in `docs/openapi.json` that were not hand-written are schema-derived (plain strings show as "string"); there is no test that every example validates against its own schema, and no consumer-driven contract test with the frontend.
11. **Cleanup behaviour.** No job or test for expired and revoked `refresh_tokens` and old `password_history` rows (they accumulate).
12. **Code issues found while writing the handover** (not fixed, docs-only task; see `docs/HANDOVER.md` Appendix B): `DEMO_MODE` and `MAX_SNAPSHOT_KB` are unused settings; structured JSON logging and metrics counters from spec 4.12 are not implemented; the entrypoint migration cannot run with a least-privilege `DATABASE_URL`.
