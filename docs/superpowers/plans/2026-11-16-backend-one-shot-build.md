# HR Assessment Backend: One-Shot Parallel Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete multi-tenant, proctored HR assessment backend (modules M1-M13 plus M5b) with tests, demo seed, Swagger and a handover document.

**Architecture:** FastAPI with routers, services and repositories per module on a frozen shared `core` layer (tenancy and RLS, auth, RBAC, flags, audit, errors, storage, AI interface). One migration owner creates the full baseline schema up front, so module agents can then work in parallel in disjoint directories.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL 16, argon2-cffi, PyJWT, cryptography, nh3, WeasyPrint, APScheduler, slowapi, pytest, Docker Compose.

**Spec:** `docs/spec/MASTER-SPEC.md` (executors read it; section numbers below refer to it). Execution model: `/Users/apple/.claude/plans/this-is-the-backend-merry-pancake.md`.

## Global Constraints

Every task's requirements include these.

- Product name comes only from `settings.app_name` (`APP_NAME`, default "HR Assessment Platform") and `settings.vendor_name` (`APP_VENDOR_NAME`, default "MRK Innovations"). No other file may contain either string. `scripts/check_name.sh` enforces this.
- Python 3.12 in Docker, PostgreSQL 16 (`citext`, `pgcrypto`), UUID ids (v7 for `audit_logs`, `proctor_events`), `timestamptz` UTC, ISO 8601 on the wire, API prefix `/api/v1`.
- Passwords: Argon2id tuned to about 250 ms. Minimum 12 characters, blocklist, no reuse of last 5. Access token 15 min, refresh rotated with reuse detection (family revoke). Lockout after 5 failures for 15 min. Default session timeout 30 min.
- Errors: envelope `{error:{code,message_key,details,request_id}}`, codes from Appendix A only, message keys `errors.<area>.<name>`, never display text. Header `X-Request-ID` on every response.
- Cross-tenant access to a known id returns **404**. Every repository query filters `org_id`, and RLS is the second layer.
- Pagination `page`, `page_size` (max 100), `total`, `sort=field:asc|desc`.
- Secrets (SMTP password, AI keys, TOTP secret, candidate email and phone, snapshots) are AES-256-GCM encrypted with a key-id prefix and are write-only through the API. Candidate email uses an HMAC-SHA-512 blind index. Invite and refresh tokens are stored only as HMAC-SHA-512 hashes.
- Audit: single write path `audit.record`, same transaction as the change, redaction of `password*`, `*_hash`, `*_enc`, `token*`, `api_key*`, `secret*`. Append-only. Retention 13 months.
- Rate limits per spec 4.7. Signed snapshot URLs last 60 s. Exam save grace is 5 s. Minimum viewport is 900 px.
- Candidate retention default 90 days, range 7-730. Max 3 assessment versions. AI generation minimum 10 questions. AI never receives candidate personal data, and report chat never receives rows.
- Every endpoint declares `x-roles` and `x-required-flag` in OpenAPI. Phase 2 items (spec 1.4) are not built.
- Tests: pytest per module (`pytest tests/mXX -q`), roughly 15-30 per module, mock AI provider only, separate test database. The full suite runs once at the end.
- Agents write only inside their own module directories (`app/modules/mXX_*`, `tests/mXX_*`, `seed/mXX_*`). `core`, `migrations` and `docker` are frozen after Task 3.

## Review Focus

Failure modes the spec implies but no task's tests exercise. Each line has a test in its owning task, tagged RF-n.

1. **RF-1 Email normalization**: `Bob@X.com ` and `bob@x.com` are the same candidate for duplicate detection. Whitespace, case and unicode are normalized before the blind index. (Task 12)
2. **RF-2 Timer races**: a double submit, or a submit racing the timeout sweeper, produces exactly one submission. Extra time granted after the deadline is rejected. A save at deadline plus 5 s is accepted and at plus 6 s is rejected. (Tasks 15, 17)
3. **RF-3 Bad answer payloads**: an empty answer, a wrong shape for the question type, an oversized payload, and a question id from another version are all rejected with a code and never a 500. (Task 15)
4. **RF-4 Foreign ids in bodies**: `assessment_version_id`, `reassign_roles_to`, and `owner_id` pointing into another organization return 404. (Tasks 5, 11)
5. **RF-5 Hostile bulk and CSV input**: BOM, malformed rows, duplicates within the file, more than 200 rows, and formula-leading cells (`=`, `+`, `-`, `@`) in the invite upload and the report CSV. `page_size=1000` is clamped or rejected. (Tasks 12, 13)

---

## Wave 0: Foundation (serial, owned by the orchestrator)

### Task 1: Skeleton, Docker, config, harness

**Files:**
- Create: `pyproject.toml`, `requirements.txt` (pinned from PyPI), `.env.example`, `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/entrypoint.sh`, `scripts/{lint,test,check_name}.sh`
- Create: `app/main.py`, `app/core/config.py`, `app/cli.py` (typer app that auto-loads every `app/modules/*/cli.py` and `app/jobs/cli.py`, each exposing `register(app: typer.Typer)`; no later task edits `cli.py`), `tests/conftest.py`, `tests/core/test_health.py`, `tests/core/test_name_config.py`

**Interfaces:**
- Produces: `settings: Settings` (fields from spec 3.5 plus `app_name`, `vendor_name`, `app_slug` derived), `create_app() -> FastAPI` which auto-registers every `app.modules.*.router.router` and sets the OpenAPI title to `settings.app_name`. `GET /health`, `/health/ready`, `/version`.
- Compose services: `api`, `worker` (same image), `db`, `test-db`, and a `dev` profile with `minio` and `mailpit`.

- [ ] **Step 1:** Write failing tests: `test_health_ok` (GET `/health` returns 200), `test_openapi_title_follows_app_name` (set `APP_NAME=Foo`, assert `app.openapi()["info"]["title"] == "Foo"`), `test_watermark_text_uses_vendor` (stub helper `core.branding_text.watermark()` returns "Powered by {vendor_name}").
- [ ] **Step 2:** Run `docker compose -f docker/docker-compose.yml run --rm api pytest tests/core -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the files above. Look up current stable versions on PyPI for every dependency and pin them. Install WeasyPrint system libraries in the Dockerfile. Use a multi-stage, non-root image with a healthcheck, and an optional `INSTALL_CLAUDE_CLI` build arg.
- [ ] **Step 4:** `check_name.sh` greps `app/ docs/ tests/` for the default name and vendor strings outside `config.py` and test fixtures. Run it and the tests. Expected: PASS.
- [ ] **Step 5:** `git add -A && git commit -m "chore: skeleton, docker, config"`

### Task 2: Core layer (B1)

**Files:**
- Create: `app/core/{db,security,errors,audit,flags,deps,pagination,ratelimit,i18n_codes,notifications,email,branding_text,export}.py`, `app/core/storage/{base,local,s3}.py`, `app/core/ai/{base,mock,registry}.py`
- `core.audit`, `core.flags`, `core.notifications` write and read with SQLAlchemy Core `text()` against table names (no ORM model imports), because Task 3 runs in parallel. Task 2 tests are pure unit tests. The DB integration test of `record()` lives in Task 3 as `tests/core/test_audit_record.py`.
- `core.export`: `csv_safe(cell) -> str` (prefix cells starting with `=`, `+`, `-`, `@`), `render_pdf(title: str, html: str) -> bytes` (WeasyPrint). `core.branding_text`: `watermark() -> str`, `STARTER_THEME: dict` (primary, accent, background_style, font_key per spec section 16.3 palette: navy, dark teal, coral, mint background).
- Test: `tests/core/test_security.py`, `test_errors.py`, `test_audit_redaction.py`, `test_storage.py`, `test_ratelimit.py`

**Interfaces (the frozen contract):**
- `core.db`: `class TenantContext(org_id: UUID|None, user_id: UUID|None, role: str, is_platform: bool, request_id: str, ip: str|None, user_agent: str|None)`. `async def tenant_session(ctx) -> AsyncIterator[AsyncSession]` runs `SET LOCAL app.org_id` and `app.is_platform` inside the transaction.
- `core.security`: `hash_password(pw)->str`, `verify_password(hash,pw)->bool`, `generate_temp_password()->str`, `create_access_token(user, sid)->str`, `decode_token(token, audience="api")->Claims` (claims sub, org_id, role, sid, tv, jti, iat, exp, iss=`settings.app_slug`), `new_opaque_token()->str`, `hmac_hash(value)->str`, `encrypt_field(plain: str)->str`, `decrypt_field(blob: str)->str`, `blind_index(email: str)->str` (normalizes by trim, lowercase and NFKC).
- `core.errors`: `class AppError(Exception)(code: str, status: int, details: dict|None=None)`, `ERROR_CODES` (Appendix A, each with a message key), the envelope handler, `VALIDATION_ERROR` mapping with `details.fields`.
- `core.audit`: `async def record(ctx, session, action: str, entity_type: str, entity_id, before=None, after=None, reason=None, result="success") -> None`, using the Appendix B action constants.
- `core.flags`: `async is_enabled(session, org_id, key)->bool`, `async effective_map(session, org_id, *, include_hidden=False)->dict[str,bool]`.
- `core.deps`: `current_ctx`, `require_role(*roles)`, `require_owner_or_admin(get_owner_id)`, `require_feature(key)` (raises `FEATURE_DISABLED` 403). Every guard adds the `x-roles` and `x-required-flag` OpenAPI extension.
- `core.notifications`: `async notify(session, org_id, user_id, type, payload, entity_type=None, entity_id=None)`.
- `core.storage`: `ObjectStorage.put(key, data, content_type, encrypt=False)`, `get(key)`, `delete(key)`, `signed_url(key, ttl_sec=60)`.
- `core.ai`: `LLMProvider.generate_json(*, system, prompt, json_schema, max_tokens, timeout_sec) -> dict`, `test_connection()`, `MockProvider(responses)`.

- [ ] **Step 1:** Write failing tests: `test_argon2_hash_verify_and_timing_budget`, `test_temp_password_meets_policy`, `test_blind_index_equal_for_case_and_whitespace_variants` (this is also RF-1 groundwork), `test_encrypt_roundtrip_and_key_rotation`, `test_audit_redacts_password_hash_token_fields`, `test_error_envelope_shape_and_request_id`, `test_local_storage_signed_url_expires`.
- [ ] **Step 2:** Run `pytest tests/core -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Key points: Argon2 parameters calibrated to about 250 ms. JWT uses an EdDSA or RS256 key with `kid`. AES-GCM blobs are `kid:nonce:ciphertext`. The rate limiter is `slowapi` behind a `RateLimiter` interface.
- [ ] **Step 4:** Run `pytest tests/core -q`. Expected: PASS.
- [ ] **Step 5:** `git commit -m "feat(core): frozen shared layer"`

### Task 3: Baseline migration, RLS, triggers (B2)

**Files:**
- Create: `app/db/models_*.py` (declarative models for every table in spec 5.3-5.5), `migrations/versions/0001_baseline.py`, `migrations/versions/0002_seed_catalogs.py`, `tests/core/test_schema.py`, `tests/core/test_rls.py`, `tests/core/test_triggers.py`, `tests/factories.py`
- Modify: `tests/conftest.py` (test DB built from migrations, per-module cleanup, token factory `make_token(role, org_id, user_id)`)

**Interfaces:**
- Produces: all models, `tests/factories.py` with `make_org`, `make_user(role)`, `make_assessment`, `make_role`, `make_invite`, `make_attempt`, and `auth_headers(role, org)`. Flag catalog and gadget catalog seeded (Appendix D and E). Roles `app_rw` (INSERT and SELECT only on `audit_logs`), `reports_ro`, and `maintenance`.
- Constraints: a partial unique index on `role_assessment_links` where `unlinked_at is null`, one open `help_requests` per invite, one draft version per assessment, a `UNIQUE(role_id, email_hash)` soft path with override, audit monthly partitions for 14 months, RLS policy per spec 4.1 on every tenant table. Triggers: published-version immutability, audit UPDATE/DELETE raise, and the protected default SA.

- [ ] **Step 1:** Write failing tests: `test_all_tables_exist`, `test_rls_blocks_cross_org_read` (session with `app.org_id=A` cannot see B rows), `test_rls_platform_mode_sees_all`, `test_audit_update_and_delete_rejected`, `test_published_version_update_rejected`, `test_default_super_admin_cannot_be_updated_or_deleted`, `test_second_active_link_per_role_rejected`.
- [ ] **Step 2:** Run `pytest tests/core/test_schema.py tests/core/test_rls.py tests/core/test_triggers.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the models and the single hand-reviewed baseline migration (partitions via SQL). Seed the 22 flags (Appendix E) and 18 gadgets (Appendix D).
- [ ] **Step 4:** Run `alembic upgrade head` on an empty DB, then the tests. Expected: PASS. **Freeze `core`, `migrations`, `docker`.**
- [ ] **Step 5:** `git commit -m "feat(db): baseline schema, RLS, triggers, catalogs"`

---

## Wave 1: Parallel modules (after Task 3)

Each task below is a fresh subagent. The brief names spec sections, the frozen interfaces from Tasks 2-3, and the directory ownership rule. Every module has: golden path, a table-driven permission test against spec 4.3, and 3-6 negatives, and ends with Swagger summaries, examples and error responses plus audit actions and flags enforced. The result is reported as files changed plus the pytest output.

### Task 4: M1 Auth and sessions (spec 6/M1, 4.2)

**Files:** `app/modules/m01_auth/{router,schemas,service,repo}.py`, `app/cli.py` (add `create-default-superadmin` only, not edit other commands), `tests/m01_auth/`
**Interfaces:**
- Produces: `AuthService.login(email, password, ctx)`, `refresh(cookie, ctx)`, `change_password(...)`, `get_me(ctx)->MeOut` (profile, flags map, branding reference).
- Consumes: `core.security`, `core.audit`, `core.flags.effective_map`.

- [ ] **Step 1:** Failing tests: `test_login_success_sets_refresh_cookie`, `test_wrong_password_generic_error_and_counter`, `test_lockout_after_5_failures_for_15_minutes`, `test_refresh_rotation_and_reuse_revokes_family`, `test_must_change_password_blocks_other_endpoints`, `test_password_history_blocks_last_5`, `test_logout_all_bumps_token_version`, `test_default_sa_login_audited_and_marked`, `test_totp_setup_verify_login`, `test_google_requires_existing_active_user_in_allowed_domain`, `test_disabled_org_login_returns_ORG_DISABLED`.
- [ ] **Step 2:** Run `pytest tests/m01_auth -q`, expect FAIL.
- [ ] **Step 3:** Implement all `/auth/*` endpoints. The Google verifier is injectable so tests mock it.
- [ ] **Step 4:** Run, expect PASS.
- [ ] **Step 5:** `git commit -m "feat(m01): auth and sessions"`

### Task 5: M2 Platform admin + M3 Team (spec 6/M2, M3)

**Files:** `app/modules/m02_platform/*`, `app/modules/m03_team/*`, `tests/m02_platform/`, `tests/m03_team/`
**Interfaces:**
- Produces: `PlatformService.create_org(payload)` (org plus first OA plus flag defaults plus starter theme, returns temp password once), `TeamService.create_user(...)`, `deactivate(user_id, reassign_roles_to)`, and `UserAdmin.last_active_admin_guard(org_id, user_id)`.

- [ ] **Step 1:** Failing tests: `test_create_org_seeds_flags_theme_and_returns_temp_password_once`, `test_oa_cannot_create_or_modify_oa_ADMIN_ONLY_ACTION`, `test_last_admin_deactivation_blocked_unless_sa_confirms`, `test_default_sa_untargetable_by_any_endpoint`, `test_flag_off_returns_FEATURE_DISABLED_and_data_kept`, `test_misuse_flag_raise_requires_comment_and_notifies_oa`, `test_misuse_clear_requires_closing_note`, `test_platform_branding_isolated_from_org_theme`, `test_deactivate_marks_owned_roles_owner_inactive`, **RF-4** `test_reassign_roles_to_user_in_other_org_is_404`, `test_team_permission_matrix`.
- [ ] **Step 2-5:** Run, implement all M2 and M3 endpoints, run again (expect PASS), commit `feat(m02-m03): platform and team`.

### Task 6: M4 Organization settings (spec 6/M4)

**Files:** `app/modules/m04_org_settings/*`, `app/modules/m04_org_settings/sanitize.py`, `app/modules/m04_org_settings/contrast.py`, `tests/m04_org_settings/`
**Interfaces:**
- Produces: `sanitize_html(html: str) -> str` (nh3 allowlist), `check_contrast(fg, bg) -> float` (WCAG ratio), `DocumentService.publish_version(doc_id, version_id)`, `AiSettingsService.get_provider_config(org_id)` (used by M10-AI).

- [ ] **Step 1:** Failing tests: `test_security_settings_validation`, `test_retention_range_7_to_730`, `test_smtp_password_write_only_masked_on_read`, `test_email_test_send_audited_and_rate_limited`, `test_ai_key_encrypted_and_masked`, `test_cli_mode_rejects_command_not_in_allowlist`, `test_branding_low_contrast_BRANDING_CONTRAST_TOO_LOW`, `test_logo_upload_checks_magic_bytes_size`, `test_document_html_sanitized_scripts_removed`, `test_document_publish_creates_immutable_version`, `test_branding_reset_copies_starter_theme`.
- [ ] **Step 2-5:** Run, implement, run (expect PASS), commit `feat(m04): org settings`.

### Task 7: M5 Assessments (spec 6/M5, 5.4)

**Files:** `app/modules/m05_assessments/{router,schemas,service,repo,scoring_defs,version_rules}.py`, `tests/m05_assessments/`
**Interfaces:**
- Produces: `AssessmentService.publish_version(...)` (validations in spec M5), `VersionRules.create_draft(assessment_id)` (3-version rule, oldest unprotected purged, else `VERSION_LIMIT_REACHED_ALL_IN_USE`), `get_published_version(org_id, version_id)` (for M6), `grade_auto(question, answer) -> Decimal` (single, multi with partial, fill_blank, code_gap per spec scoring definitions; used by M9).

- [ ] **Step 1:** Failing tests: `test_create_assessment_makes_v1_draft`, `test_publish_validations` (table: no questions, zero marks, one option, no correct, blank without answers, AI question without model answer gives `MODEL_ANSWER_REQUIRED`), `test_published_version_immutable_ASSESSMENT_IMMUTABLE`, `test_fourth_version_purges_oldest_unprotected`, `test_all_three_protected_blocks_creation`, `test_one_draft_per_assessment_DRAFT_ALREADY_EXISTS`, `test_delete_blocked_while_linked_to_open_role`, `test_preview_hides_answer_keys`, `test_grade_auto_multi_choice_all_or_nothing_and_partial`, `test_fill_blank_case_insensitive_trimmed`, `test_non_owner_cannot_edit`.
- [ ] **Step 2-5:** Run, implement, run (expect PASS), commit `feat(m05): assessments`.

### Task 8: M5b Question bank (spec 5.5 `question_bank_items`, flag `question_bank`)

**Files:** `app/modules/m05b_question_bank/*`, `tests/m05b_question_bank/`
**Interfaces:** Produces `POST/GET/PATCH/DELETE /question-bank` and `GET /question-bank/{id}/as-question`, which returns a body in the shape of M5's create-question request (the frontend posts it to M5). It does not call M5 (Wave 1 runs in parallel).

- [ ] **Step 1:** Failing tests: `test_save_question_to_bank`, `test_bank_is_org_scoped`, `test_as_question_returns_create_question_shape`, `test_flag_off_blocks_bank`, `test_tag_filter`.
- [ ] **Step 2-5:** Run, implement, run (expect PASS), commit `feat(m05b): question bank`.

### Task 9: M10 Audit APIs and exports (spec 6/M10, 4.5)

**Files:** `app/modules/m10_audit/*`, `app/modules/m10_audit/export.py`, `tests/m10_audit/`
**Interfaces:** Produces `AuditQuery.search(ctx, filters, page)`, `export_audit(ctx, columns, fmt)` (CSV with injection guard `csv_safe(cell)`, PDF via WeasyPrint with a header from `settings.app_name`), `GET /audit-logs/security-events`.

- [ ] **Step 1:** Failing tests: `test_oa_sees_only_own_org_rows`, `test_sa_platform_view_all_orgs_read_only`, `test_export_is_itself_audited`, `test_csv_formula_injection_prefixed`, `test_audit_table_rejects_update_via_api_role`, `test_filters_actor_action_date`, `test_hr_forbidden`.
- [ ] **Step 2-5:** Run, implement, run (expect PASS), commit `feat(m10): audit`.

### Task 10: AI provider layer (spec 7.1, 7.2, B10 part 1)

**Files:** `app/modules/m14_ai/{providers/{anthropic,gemini,openai,compatible,cli}.py,prompts/question_gen_v1.py,service.py,router.py,ai_models.json}`, `tests/m14_ai/`
**Interfaces:**
- Produces: `AiService.generate_questions(ctx, req: QuestionGenRequest) -> list[SuggestedQuestion]` (stateless, not stored, at least 10, dedupes, one repair retry), `AiService.score_answer(ctx, question, model_answer, marks, answer_text) -> ScoreResult(fraction, reason, matched, missed)` (never sends personal data), `get_provider(org_ai_settings) -> LLMProvider`. `POST /assessments/ai/generate` and `GET /assessments/ai/options`.
- CLI provider uses `asyncio.create_subprocess_exec` with an argument list, the prompt on stdin, a timeout, a sanitized environment, and a temp working directory.

- [ ] **Step 1:** Failing tests (mock provider only): `test_generate_returns_at_least_10_and_none_stored`, `test_jd_wrapped_as_untrusted_data_in_prompt`, `test_invalid_json_repaired_once_then_dropped`, `test_cli_provider_uses_arg_list_not_shell`, `test_cli_command_outside_allowlist_rejected`, `test_monthly_cap_AI_LIMIT_REACHED`, `test_unconfigured_AI_NOT_CONFIGURED`, `test_audit_has_counts_not_content`, `test_score_answer_clamps_fraction_and_sends_no_pii`.
- [ ] **Step 2:** Run `pytest tests/m14_ai -q`, expect FAIL.
- [ ] **Step 3:** Implement. Verify current Gemini Flash-class and OpenAI mini-class model ids online and write them to `ai_models.json` (Claude default `claude-haiku-5-5`).
- [ ] **Step 4:** Run, expect PASS.
- [ ] **Step 5:** `git commit -m "feat(m14): ai providers and generation"`

**Wave 1 gate:** review pass over Tasks 4-10, then `scripts/check_name.sh`.

---

## Wave 2

### Task 11: M6 Open roles (spec 6/M6)

**Files:** `app/modules/m06_roles/*`, `tests/m06_roles/`
**Interfaces:** Produces `RoleService.get_open_linked_role(org_id, role_id) -> (Role, AssessmentVersion)` (raises `ROLE_NOT_OPEN` / `ROLE_HAS_NO_ASSESSMENT`), `link_assessment(...)`, `close(role_id)` (calls the registry in `app/modules/m06_roles/hooks.py: on_role_close: list[Callable[[UUID], Awaitable[None]]]`; Task 12 registers `InviteService.revoke_unstarted`).

- [ ] **Step 1:** Failing tests: `test_link_replace_keeps_pending_invites_on_old_version`, `test_one_active_link_per_role`, `test_only_published_version_linkable_ASSESSMENT_NOT_PUBLISHED`, **RF-4** `test_link_version_from_other_org_is_404`, `test_delete_only_draft_without_invites`, `test_reassign_owner_in_other_org_404`, `test_close_revokes_unstarted_not_in_progress`, `test_non_owner_cannot_edit_role`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m06): roles`.

### Task 12: M7 Invites, help requests, accommodations (spec 6/M7)

**Files:** `app/modules/m07_invites/{router,schemas,service,repo,csv_import}.py`, `tests/m07_invites/`
**Interfaces:** Produces `InviteService.create(ctx, role_id, payload) -> (Invite, exam_url)`, `revoke_unstarted(role_id)`, `regenerate_link`, `reset_binding`, `HelpRequestService.approve/decline`. Exam URL is `{settings.app_base_url}/exam/{token}`. Consumes `RoleService.get_open_linked_role`, `core.security.blind_index/encrypt_field/hmac_hash`.

- [ ] **Step 1:** Failing tests: `test_create_returns_url_once_stores_only_hash`, `test_duplicate_email_409_unless_allow_duplicate`, **RF-1** `test_duplicate_detected_across_case_and_whitespace_variants`, `test_regenerate_kills_old_link`, `test_requires_open_role_and_linked_assessment`, `test_invite_records_version_at_creation`, `test_extend_requires_reason`, `test_send_email_requires_verified_smtp_SMTP_NOT_CONFIGURED`, `test_help_request_one_open_per_invite`, `test_approve_sets_exemption_and_extra_minutes_audited`, **RF-5** `test_csv_bulk_handles_bom_bad_rows_in_file_duplicates_over_200_rows_and_formula_cells`, `test_non_owner_hr_sees_limited_fields`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m07): invites`.

### Task 13: M11 Report-chat engine (spec 6/M11, 7.4, Appendix F)

**Files:** `app/modules/m11_reports/{registry,filterspec,compiler,executor,exporters,service,router}.py`, `tests/m11_reports/`
**Interfaces:** Produces `FIELD_CATALOG: dict[source, list[FieldDef]]` (candidates, roles, assessments, team, audit), `FilterSpec` (Pydantic), `validate_spec(spec) -> FilterSpec` (raises `REPORT_FILTER_INVALID`), `compile_spec(spec, org_id) -> (sql, params)`, `run_readonly(sql, params)` (uses `DATABASE_URL_REPORTS_RO`, read-only transaction, statement timeout, row cap). Consumes `AiService` through `get_provider` for interpretation, and the AI sees only the catalog.

- [ ] **Step 1:** Failing tests: `test_prompt_contains_catalog_never_rows`, `test_unknown_field_op_or_value_rejected`, `test_compiler_forces_org_id_and_parameterizes`, `test_estimated_count_comes_from_db_not_ai`, `test_readonly_role_cannot_write`, **RF-5** `test_csv_cells_starting_with_formula_chars_prefixed`, `test_pdf_header_uses_app_name`, `test_report_audit_stores_spec_not_rows`, `test_sa_reports_exclude_candidate_content`, `test_hr_forbidden`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m11): report chat`.

### Task 14: M12 Notifications and gadget framework (spec 6/M12, 4.10, Appendix C and D)

**Files:** `app/modules/m12_dashboard/{router,service,gadgets/{__init__,base,platform_gadgets}.py}`, `tests/m12_dashboard/`
**Interfaces:** Produces `Gadget` protocol `data(ctx) -> dict`, a registry decorator `@register_gadget(key)`, and the notification endpoints. Gadgets needing later data are registered as `{"pending": true}` stubs in `gadgets/stubs.py`, which Task 18 deletes and replaces.

- [ ] **Step 1:** Failing tests: `test_layout_saved_per_user`, `test_gadget_not_for_role_absent_from_catalog_and_rejected`, `test_unread_count_and_mark_read_all`, `test_notifications_are_user_scoped`, `test_hr_sees_own_scope_oa_org_scope_sa_platform_only`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m12): dashboard framework`.

**Wave 2 gate:** review pass, then `check_name.sh`.

---

## Wave 3

### Task 15: M8 Exam engine and proctoring (spec 6/M8, section 8)

**Files:** `app/modules/m08_exam/{router,schemas,service,repo,session,severity_rules,device}.py`, `tests/m08_exam/`
**Interfaces:** Produces exam-session JWT (audience `exam`, bound to attempt, max 12 h), `ExamService.start(ctx)` (checks consent, camera, help requests, flags), `heartbeat`, `save_answer`, `ingest_events` (max 50 per call), `submit(reason)`, `severity_for(event_type, meta) -> str` (server rules), `attempt_remaining_seconds(attempt, now)`. Consumes M7 invite lookups, M5 `get_published_version`.

- [ ] **Step 1:** Failing tests: `test_open_binds_browser_and_other_browser_gets_LINK_BOUND_TO_OTHER_SESSION`, `test_phone_ua_or_narrow_viewport_DEVICE_NOT_SUPPORTED`, `test_start_requires_consent_camera_and_no_pending_help`, `test_deadline_is_start_plus_duration_times_multiplier_plus_extra`, `test_heartbeat_reports_server_remaining_and_never_pauses`, `test_questions_exclude_answer_keys_and_only_after_start`, `test_server_assigns_severity_ignoring_client_value`, `test_three_tab_switches_escalate_to_serious`, `test_violations_never_stop_or_reject_the_exam`, `test_flag_counts_updated_on_ingest`, `test_snapshot_rate_limits`, `test_after_submit_session_read_only`, **RF-2** `test_double_submit_yields_one_submission`, `test_save_accepted_at_grace_plus_5s_rejected_at_plus_6s`, **RF-3** `test_empty_wrong_shape_oversized_and_foreign_question_answers_rejected_with_codes`, `test_flags_checked_at_start_not_continuously`.
- [ ] **Step 2-5:** Run `pytest tests/m08_exam -q`, implement, run, commit `feat(m08): exam engine`.

---

## Wave 4

### Task 16: M9 Review, decisions, scoring (spec 6/M9, 7.3 auto part)

**Files:** `app/modules/m09_review/*`, `tests/m09_review/`
**Interfaces:** Produces `app/modules/m09_review/hooks.py` with `ai_scoring_hooks: list[Callable[[UUID], Awaitable[None]]]` (called by `ScoringService` for attempts that have AI-scored answers, and by the `retry_ai_scoring` job; Task 19 registers its runner). Also `ScoringService.score_attempt(attempt_id)` (auto first, sets `scoring_status`, queues AI answers), `ReviewService.get_report(ctx, attempt_id)` (audited view), `snapshot_url(ctx, attempt_id, asset_id)` (60 s, audited), `decide(ctx, attempt_id, decision, note)`, `override_decision(...)`, `erase_candidate_data(ctx, attempt_id, include_identity=False)`.

- [ ] **Step 1:** Failing tests: `test_report_view_is_audited`, `test_non_owner_hr_sees_name_status_decision_only`, `test_sa_cannot_see_answers_or_snapshots`, `test_decision_allowed_once_DECISION_ALREADY_MADE`, `test_oa_override_needs_reason_and_keeps_history`, `test_manual_score_override_audited`, `test_extra_time_only_while_in_progress`, `test_dismiss_flag_with_note`, `test_erase_removes_answers_snapshots_events_phone`, `test_score_percent_and_passed_computed_from_version_pass_mark`, `test_decision_requires_submitted_attempt`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m09): review`.

### Task 17: Background jobs and notification triggers (spec 9, 4.10)

**Files:** `app/jobs/{scheduler,expire_invites,timeout_attempts,retry_ai_scoring,retention_purge,version_purge,audit_partitions,notification_cleanup,report_cleanup}.py`, `app/jobs/__init__.py` (registry `JOBS: dict[str, Callable]`), `tests/jobs/`
**Interfaces:** Each job is `async def run(session_factory, now=None) -> dict` (idempotent, platform-mode, logged). `python -m app.cli run-job <name>` dispatches via `JOBS`.

- [ ] **Step 1:** Failing tests: **RF-2** `test_sweeper_and_candidate_submit_race_produces_one_submission`, `test_timeout_submits_past_deadline_plus_grace_with_reason_timer`, `test_expire_invites_marks_overdue_and_notifies_24h_ahead`, `test_retention_purge_deletes_detail_keeps_summary_and_flag_counts`, `test_undecided_attempts_never_purged`, `test_audit_partitions_create_next_drop_older_than_13_months_and_log_purge`, `test_jobs_idempotent_second_run_noop`, `test_serious_flag_notification_debounced_10_min`, `test_run_job_cli_dispatch`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(jobs): scheduler and jobs`.

### Task 18: Gadget data (spec Appendix D)

**Files:** `app/modules/m12_dashboard/gadgets/{hr_gadgets,oa_gadgets,sa_gadgets}.py`, `tests/m12_dashboard/test_gadget_data.py`
**Interfaces:** Registers all 18 gadget keys with scoped data (HR own roles, OA org, SA platform counts only, no candidate data).

- [ ] **Step 1:** Failing tests: `test_each_catalog_key_has_registered_gadget`, `test_hr_gadget_counts_only_own_roles`, `test_sa_gadgets_contain_no_candidate_fields`, `test_needs_attention_lists_undecided_past_retention`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(m12): gadget data`.

---

## Wave 5

### Task 19: AI scoring integration

**Files:** `app/modules/m14_ai/scoring_runner.py`, `tests/m14_ai/test_scoring_runner.py`
**Interfaces:** `run_ai_scoring(attempt_id)` consumes `AiService.score_answer`, writes `ai_marks` and `ai_reason`, sets `needs_manual` and notifies on failure. Wire it into `ScoringService` and `retry_ai_scoring` through their existing hooks only (no edits to module files owned by other tasks; use the registered hook).

- [ ] **Step 1:** Failing tests: `test_ai_marks_stored_with_reason`, `test_provider_missing_sets_needs_manual_and_notifies`, `test_ai_scoring_flag_off_skips_to_manual`, `test_repeated_failure_marks_needs_manual`, `test_audit_AI_SCORING_RUN_has_counts_only`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(ai): scoring runner`.

### Task 20: Security suite (spec section 11)

**Files:** `tests/security/{test_isolation,test_permission_matrix,test_admin_rules,test_timer,test_invite_links,test_audit_append_only,test_flag_blocking}.py`, `tests/security/helpers.py`
**Interfaces:** `iterate_tenant_endpoints(app)` yields every route with the `x-roles` extension and a flag. Filterable with `-k` by module.

- [ ] **Step 1:** Write tests: `test_org_a_token_never_reads_lists_or_changes_org_b` (parametrized over all tenant routes, expects 404), `test_matrix_every_route_rejects_each_disallowed_role_per_spec_4_3`, `test_last_admin_and_default_sa_protected`, `test_server_clock_ignores_client_time`, `test_link_expiry_binding_regeneration`, `test_flag_off_blocks_api_for_every_flagged_route`, `test_every_route_has_x_roles_and_x_required_flag`.
- [ ] **Step 2:** Run `pytest tests/security -q`. Failures are routed back to the owning module task for a fix (never patched in the test).
- [ ] **Step 3-5:** Re-run until PASS and commit `test(security): isolation and permission suite`.

**Wave 5 gate:** review pass over Tasks 15-20.

---

## Wave 6: Finish

### Task 21: End-to-end scenario and full pass

**Files:** `tests/e2e/test_full_flow.py`
**Steps:**
- [ ] **Step 1:** Write `test_full_flow`: create org, create HR, create assessment with all 5 question types, publish, create and open role, link, invite, open exam session, consent, system check, baseline photo, start, save answers, post events, submit, score, review, decide, create a report through the mock AI, export the audit.
- [ ] **Step 2:** Wipe the dev DB, then run `pytest -q`. Expected: all green, including `tests/security`. Fix failures in the owning module.
- [ ] **Step 3:** Commit `test(e2e): full flow`.

### Task 22: Seed demo (spec section 12)

**Files:** `seed/{demo.py,data.py}`, `app/cli.py` (commands `seed-demo`, `reset-demo`), `tests/seed/test_seed.py`
**Interfaces:** Drives the service layer. Creates the two orgs, 4 assessments (one with 2 versions), about 6 roles, about 20 invites and attempts, flagged `is_demo`.

- [ ] **Step 1:** Failing tests: `test_reset_demo_is_repeatable_and_counts_match_plan`, `test_demo_accounts_can_login`, `test_demo_rows_flagged_is_demo`.
- [ ] **Step 2-5:** Run, implement, run, commit `feat(seed): demo data`.

### Task 23: OpenAPI export and Swagger audit

**Files:** `app/cli.py` (`export-openapi`), `docs/openapi.json`, `tests/core/test_openapi.py`
- [ ] **Step 1:** Failing tests: `test_every_operation_has_summary_description_examples_errors_x_roles_x_required_flag`, `test_security_schemes_bearer_and_exam_session`.
- [ ] **Step 2-5:** Run, fix gaps in the module routers, export `docs/openapi.json`, commit `docs: openapi`.

### Task 24: Handover document

**Files:** `docs/HANDOVER.md`, `docs/error_codes.md`, `docs/test_report.md`
**Content:** The 12 sections in spec 15, plus "How to rename the product" (`APP_NAME`, `APP_VENDOR_NAME`) and an AI-review note on the diagrams.
- [ ] **Step 1:** Generate sections from the code where possible (flag catalog, error codes, endpoint index by module from OpenAPI, job schedules).
- [ ] **Step 2:** Verify the rename procedure by running it: set `APP_NAME=Foo`, then check the OpenAPI title and watermark text.
- [ ] **Step 3:** Commit `docs: handover`.

---

## Self-Review

- **Spec coverage:** M1 to Task 4; M2/M3 to Task 5; M4 to Task 6; M5 to Task 7; M5b to Task 8; M10 to Task 9; AI (7.1, 7.2) to Task 10 and 7.3 to Tasks 16 and 19; M6 to Task 11; M7 to Task 12; M11 to Task 13; M12 to Tasks 14 and 18; M8 to Task 15; M9 to Task 16; jobs (section 9) to Task 17; M13 and CLI to Tasks 1, 4, 17, 22 and 23; schema, RLS and triggers to Task 3; security suite to Task 20; seed to Task 22; Swagger and handover to Tasks 23 and 24. Docker and deployment (section 13) is in Task 1.
- **Known gaps to watch:** the Google sign-in domain default (spec 16.3) is resolved as "empty list means Google sign-in is off for that organization" (my call, to be confirmed). The PDF renderer interface is WeasyPrint-only for now.
- **Type consistency:** the `TenantContext`, `AppError`, `audit.record` and `require_*` names in Task 2 are the only ones modules consume. The service interfaces that cross modules are `RoleService.get_open_linked_role`, `InviteService.revoke_unstarted`, `get_published_version`, `grade_auto`, `AiService.score_answer` and `ScoringService.score_attempt`.
- **Proportion:** this plan is shorter than the spec. The spec holds all endpoint detail and each task points to its section instead of repeating it.
