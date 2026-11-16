# app.core: the frozen shared contract

Modules (`app/modules/mXX_*`) build on these functions only. Do not edit `app/core`
after Task 3; ask the orchestrator if something is missing. Spec references are to
`docs/spec/MASTER-SPEC.md`.

## Using guards in a router

```python
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends
from app.core import audit
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_owner_or_admin, require_role
from app.core.roles import HR_USER, ORG_ADMIN

# Router-level flag guard: every route below gets x-required-flag "roles".
router = APIRouter(prefix="/api/v1/roles", tags=["roles"],
                   dependencies=[Depends(require_feature("roles"))])

Staff = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]

@router.post("")
async def create_role(body: RoleIn, ctx: Staff, session: DbSession):
    role = await service.create(ctx, session, body)          # repo filters org_id = ctx.org_id
    await audit.record(ctx, session, audit.ROLE_CREATED, "open_role", role.id, after=body.model_dump())
    return role

async def role_owner(role_id: UUID, ctx: Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))],
                     session: DbSession) -> UUID | None:
    return await repo.owner_id(session, ctx.org_id, role_id)   # None when not in caller's org -> 404

@router.patch("/{role_id}")
async def edit_role(role_id: UUID, body: RoleUpdate, session: DbSession,
                    ctx: Annotated[TenantContext, Depends(require_owner_or_admin(role_owner))]): ...
```

- `x-roles` / `x-required-flag` are generated from the guards by `install_openapi_extensions`
  (called in `create_app`). Several role guards intersect. Routes without a guard are
  public and get `x-roles: []`, `x-required-flag: null`. Public routes should also say so
  explicitly: `openapi_extra={"x-roles": [], "x-required-flag": None}`.
- `DbSession` is the request transaction (RLS set from the context). It commits **before**
  the response is sent (`Depends(scope="function")`) and rolls back on any exception.
  Never call `session.commit()` in services. Audit rows go in the same transaction.
- Raise `AppError("CODE")` for every error. Cross-tenant ids return `AppError("NOT_FOUND")`.
- Super admins (`org_id=None`, `is_platform=True`) are not flag-gated by `require_feature`.

## Public API by module

### core.db
- `TenantContext(org_id: UUID|None, user_id: UUID|None, role: str, is_platform: bool, request_id: str, ip: str|None, user_agent: str|None, actor_type="user", actor_name=None, session_id=None)` (frozen). `.for_org(org_id)`.
- `system_context(request_id="system", org_id=None) -> TenantContext` (jobs, CLI; platform mode, actor_type `system`).
- `async with tenant_session(ctx) as session` -> one transaction; sets `app.org_id` and `app.is_platform` via `set_config(..., true)` (= `SET LOCAL`); commits on exit, rolls back on error.
- `async with reports_session(ctx, statement_timeout_ms=15000)`: read-only transaction on `DATABASE_URL_REPORTS_RO`, always rolled back.
- `get_engine()`, `get_sessionmaker()`, `apply_tenant_settings(session, ctx)`.

### core.deps
- `current_ctx` (staff, enforces active user, `tv`, active org, no pending password change), `current_ctx_allow_password_change`.
- `DbSession` (Annotated), `db_session`.
- `require_role(*roles)`, `require_owner_or_admin(get_owner_id)`, `require_feature(key)` (403 `FEATURE_DISABLED`, `details.flag`). All return the `TenantContext`.
- Public: `public_ctx` (anonymous, platform mode: filter org explicitly), `public_db_session`.
- Candidate: `current_exam_ctx -> ExamContext(ctx, attempt_id, invite_id, binding)` (audience `exam`; M8 compares the binding cookie), `exam_db_session`.
- `load_principal(claims)` (monkeypatch in tests), `install_openapi_extensions(app)`, `guard_extensions(route)`.

### core.security
- `hash_password(pw) -> str`, `verify_password(hash, pw) -> bool`, `password_needs_rehash(hash)`, `dummy_verify(pw)` (unknown user, constant time).
- `password_policy_violations(pw, *, min_length=12, blocklist=COMMON_PASSWORDS) -> list[str]` (`too_short`, `common_password`; reuse-of-last-5 is the auth service's job). `generate_temp_password() -> str` (16 chars, all classes).
- `create_access_token(user, sid) -> str` (user has `id, org_id, role, token_version`), `create_exam_token(*, attempt_id, invite_id, org_id, binding, ttl_hours=12) -> str`, `decode_token(token, audience="api") -> Claims` (raises `UNAUTHENTICATED`). `Claims(sub, org_id, role, sid, tv, jti, iat, exp, iss, aud, binding)`; `iss = settings.app_slug`; header `kid = JWT_KID`; EdDSA (Ed25519) or RS256 from the key type.
- `new_opaque_token() -> str` (32 bytes urlsafe), `hmac_hash(value) -> str` (HMAC-SHA-512 hex, `TOKEN_PEPPER`), `normalize_email(email)`, `blind_index(email) -> str` (NFKC, trim, lowercase, then HMAC).
- `encrypt_field(plain) -> "kid:nonce:ciphertext"`, `decrypt_field(blob) -> str` (ValueError on unknown kid/tamper), `encrypt_bytes`, `decrypt_bytes`, `mask_secret(value)`.
- Argon2id cost: `ARGON2_TIME_COST=12`, `ARGON2_MEMORY_KIB=65536`, `ARGON2_PARALLELISM=4` (about 210-290 ms measured in the api container). Test suites that create many users may lower them on `settings`.

### core.errors / core.i18n_codes
- `AppError(code, status=None, details=None)` (status defaults to the catalog; unknown codes raise ValueError). `validation_error(fields)`.
- `ERROR_CODES: dict[str, ErrorSpec(code, message_key, status)]`, exactly Appendix A. `VALIDATION_ERROR`.
- `install_error_handlers(app)`: envelope `{error:{code,message_key,details,request_id}}` for AppError, request validation (`details.fields=[{field, code}]`), HTTP errors and unhandled errors.
- Unhandled exceptions return 500 with code `INTERNAL_ERROR` / `errors.common.internal`. This is the one code outside Appendix A (Appendix A has no 500 code); it is not in `ERROR_CODES`, so services cannot raise it.

### core.audit
- `async record(ctx, session, action, entity_type, entity_id, before=None, after=None, reason=None, result="success")`. Inserts with `text()`, `id` is UUIDv7, `entity_id` stored as text. Redacts `password*`, `*_hash`, `*_enc`, `token*`, `api_key*`, `secret*` and `REDACT_EXTRA` at any depth; adds `after["_redacted_changed"] = [fields]` when a redacted value changed. `result` in `success|failure|denied`.
- One constant per Appendix B action (`audit.USER_CREATED`, ...), `AUDIT_ACTIONS`, `redact(value)`, `is_sensitive(key)`.

### core.flags
- `async is_enabled(session, org_id, key) -> bool`, `async effective_map(session, org_id, *, include_hidden=False) -> dict[str, bool]`. Constants per Appendix E key, `FLAG_KEYS`, `FLAG_DEPENDENCIES` (`invites` needs `roles`, `assessments`; `csv_bulk_invite` needs `invites`).

### core.notifications
- `async notify(session, org_id, user_id, type, payload, entity_type=None, entity_id=None)`. Constants per Appendix C type, `NOTIFICATION_TYPES`. Debounce in the caller.

### core.pagination
- `page_params` dependency -> `PageParams(page, page_size, sort)` with `.offset`, `.limit` (`page_size` max 100, larger is `VALIDATION_ERROR`).
- `parse_sort(sort, allowed, default=None) -> list[(field, "asc"|"desc")]`. `Page[T].build(items, total=, params=)`.

### core.ratelimit
- `RateLimiter` protocol (`hit`, `retry_after`, `reset`), `InMemoryRateLimiter` (on `limits`, the slowapi engine), `get_rate_limiter()`, `set_rate_limiter()`.
- `check_rate_limit(limit, *keys)` raises `RATE_LIMITED` with `details.retry_after_sec` (and a `Retry-After` header).
- `rate_limit(limit | callable, *, scope="ip"|"user"|"org", name)` dependency; for `user`/`org`, put the auth guard first. Use `lambda: settings.rate_limit_login` so tests can change settings.

### core.storage
- `get_storage() -> ObjectStorage`, `set_storage(obj | None)`.
- `ObjectStorage.put(key, data, content_type, encrypt=False) -> StoredObject(key, content_type, size_bytes, sha256, encrypted)`, `get(key) -> bytes` (decrypts), `delete(key)`, `signed_url(key, ttl_sec=60) -> str`, `content_type(key)`, `verify_signed_token(token)`.
- Keys: `<org_id>/<kind>/<uuid>.<ext>`; unsafe keys are `UPLOAD_INVALID`. Signed URLs for every driver point to `GET /api/v1/files/{token}` (core router, public, token = authorization), so encrypted snapshots are decrypted server-side. Authorize and `audit.record(SNAPSHOT_VIEWED)` before calling `signed_url`. `S3Storage.presigned_url` is for unencrypted objects only.

### core.ai
- `LLMProvider` protocol: `generate_json(*, system, prompt, json_schema, max_tokens, timeout_sec) -> dict`, `test_connection() -> bool`.
- `MockProvider(responses)`: dicts returned in order (last repeats), exception instances raised, callables called with the kwargs; `.calls`.
- `ProviderConfig(provider, model, mode="api", base_url=None, api_key=None, cli_command_key=None, timeout_sec=60)`, `register_provider(name, factory)`, `build_provider(config)` (`AI_NOT_CONFIGURED` when unknown), `set_provider_override(provider)`.

### core.email
- `SmtpConfig`, `OutgoingEmail(to, subject, html, text=None)`, `get_email_sender().send(smtp, message)` (`SMTP_NOT_CONFIGURED` when `smtp is None`), `MemoryEmailSender` for tests via `set_email_sender`.

### core.export / core.branding_text
- `csv_safe(cell) -> str` (prefixes `= + - @` tab CR with `'`; numbers untouched), `csv_bytes(headers, rows) -> bytes` (UTF-8 BOM).
- `render_pdf(title, html) -> bytes` (WeasyPrint; only `data:` URIs are fetched; sanitize `html` first).
- `watermark() -> "Powered by {settings.vendor_name}"`, `STARTER_THEME` (primary navy, secondary dark teal, accent coral, highlight logo teal, background mint, `background_style`, `font_key`), `FONT_KEYS`, `BACKGROUND_STYLES`, `contrast_ratio(a, b)`.

### Other
- `core.roles`: `SUPER_ADMIN`, `ORG_ADMIN`, `HR_USER`, `CANDIDATE`, `SYSTEM`, `STAFF_ROLES`.
- `core.middleware.safe_request_id(value)`: incoming `X-Request-ID` is reused only if pure ASCII and fully matching `[A-Za-z0-9._-]{1,64}`.
- `core.testing.configure_test_secrets(settings)`: fills empty JWT keys (Ed25519), `FIELD_ENCRYPTION_KEYS`, `TOKEN_PEPPER` for tests. The root `tests/conftest.py` should call it so every module's tests have keys.
