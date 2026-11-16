# Error code catalog

Generated from the `ERROR_CODES` registry (`app/core/i18n_codes.py`, exposed by `app/core/errors.py`): 44 codes that
services may raise with `AppError("CODE")`, plus one handler-only code (`INTERNAL_ERROR`). The backend returns **codes and
message keys only**; the frontend maps each `message_key` to translated text in its `en.json`.

To regenerate the table from the code:

    docker compose -f docker/docker-compose.yml run --rm api python -c "from app.core.errors import ERROR_CODES; [print(s.code, s.status, s.message_key) for s in ERROR_CODES.values()]"

## Envelope

Every error response, on every endpoint, has the same shape:

```json
{ "error": { "code": "DUPLICATE_INVITE", "message_key": "errors.invite.duplicate",
             "details": {"existing_invite_id": "..."}, "request_id": "..." } }
```

- `request_id` equals the `X-Request-ID` response header. Quote it in support requests; it is the key into the logs and the audit log.
- `details` is always an object (possibly empty). Known members: `fields` (validation), `flag` (`FEATURE_DISABLED`),
  `retry_after_sec` (`RATE_LIMITED`, also sent as a `Retry-After` header), `existing_invite_id` (`DUPLICATE_INVITE`),
  `reason` (for example `UPLOAD_INVALID`, `CONFLICT`), `max_rows` (export caps).
- Cross-organization ids always answer `404 NOT_FOUND` (never 403), so ids cannot be probed.

## Handler-only code (not in the registry)

| Code | HTTP | Message key | When |
|---|---|---|---|
| `INTERNAL_ERROR` | 500 | `errors.common.internal` | Any unhandled exception. Services cannot raise it (decision recorded in the build ledger: the spec's Appendix A has no 500 code). |

## Validation errors

`VALIDATION_ERROR` (422) carries `details.fields = [{"field": "<path>", "code": "<field code>"}]`. Field codes are mapped from
Pydantic error types and are also message keys for the frontend:

| Field code | Meaning |
|---|---|
| `required` | missing value |
| `too_short`, `too_long` | string or list length |
| `too_small`, `too_large` | number bounds |
| `invalid_format` | pattern mismatch |
| `invalid_choice` | not one of the allowed values |
| `invalid_type` | wrong JSON type (int, bool, string, uuid, datetime, date) |
| `invalid_json` | body is not valid JSON |
| `unknown_field` | extra field on a strict body |
| `invalid` | any other value error |

Modules add their own field codes where a rule is business specific (for example `too_many_rows`, `unresolvable`,
`internal_address_not_allowed`, `required`, `not_allowed`); treat an unknown field code as a generic "invalid" message.

## Framework errors

HTTP errors raised by the framework are mapped to catalog codes: 400/422 `VALIDATION_ERROR`, 401 `UNAUTHENTICATED`, 403 `FORBIDDEN`,
404 and 405 `NOT_FOUND`, 409 `CONFLICT`, 413 and 415 `UPLOAD_INVALID`, 429 `RATE_LIMITED`.

## Catalog by area

### ai

| Code | HTTP | Message key |
|---|---|---|
| `AI_LIMIT_REACHED` | 429 | `errors.ai.limit_reached` |
| `AI_NOT_CONFIGURED` | 409 | `errors.ai.not_configured` |
| `AI_OUTPUT_INVALID` | 502 | `errors.ai.output_invalid` |
| `AI_PROVIDER_ERROR` | 502 | `errors.ai.provider_error` |

### assessment

| Code | HTTP | Message key |
|---|---|---|
| `ASSESSMENT_IMMUTABLE` | 409 | `errors.assessment.immutable` |
| `ASSESSMENT_NOT_PUBLISHED` | 409 | `errors.assessment.not_published` |
| `DRAFT_ALREADY_EXISTS` | 409 | `errors.assessment.draft_already_exists` |
| `VERSION_LIMIT_REACHED_ALL_IN_USE` | 409 | `errors.assessment.version_limit_all_in_use` |

### auth

| Code | HTTP | Message key |
|---|---|---|
| `ACCOUNT_LOCKED` | 423 | `errors.auth.account_locked` |
| `INVALID_CREDENTIALS` | 401 | `errors.auth.invalid_credentials` |
| `MUST_CHANGE_PASSWORD` | 403 | `errors.auth.must_change_password` |
| `PASSWORD_POLICY_FAILED` | 422 | `errors.auth.password_policy_failed` |

### branding

| Code | HTTP | Message key |
|---|---|---|
| `BRANDING_CONTRAST_TOO_LOW` | 422 | `errors.branding.contrast_too_low` |

### common

| Code | HTTP | Message key |
|---|---|---|
| `ADMIN_ONLY_ACTION` | 403 | `errors.common.admin_only_action` |
| `CONFLICT` | 409 | `errors.common.conflict` |
| `FEATURE_DISABLED` | 403 | `errors.common.feature_disabled` |
| `FORBIDDEN` | 403 | `errors.common.forbidden` |
| `NOT_FOUND` | 404 | `errors.common.not_found` |
| `RATE_LIMITED` | 429 | `errors.common.rate_limited` |
| `UNAUTHENTICATED` | 401 | `errors.common.unauthenticated` |
| `VALIDATION_ERROR` | 422 | `errors.common.validation` |

### email

| Code | HTTP | Message key |
|---|---|---|
| `SMTP_NOT_CONFIGURED` | 409 | `errors.email.smtp_not_configured` |

### exam

| Code | HTTP | Message key |
|---|---|---|
| `CAMERA_REQUIRED` | 409 | `errors.exam.camera_required` |
| `CONSENT_REQUIRED` | 409 | `errors.exam.consent_required` |
| `DEVICE_NOT_SUPPORTED` | 422 | `errors.exam.device_not_supported` |
| `EXAM_ALREADY_SUBMITTED` | 409 | `errors.exam.already_submitted` |
| `EXAM_NOT_STARTED` | 409 | `errors.exam.not_started` |
| `EXAM_TIME_OVER` | 409 | `errors.exam.time_over` |
| `HELP_REQUEST_PENDING` | 409 | `errors.exam.help_request_pending` |
| `LINK_BOUND_TO_OTHER_SESSION` | 403 | `errors.exam.link_bound_to_other_session` |

### invite

| Code | HTTP | Message key |
|---|---|---|
| `DUPLICATE_INVITE` | 409 | `errors.invite.duplicate` |
| `INVITE_EXPIRED` | 410 | `errors.invite.expired` |
| `INVITE_REVOKED` | 410 | `errors.invite.revoked` |

### org

| Code | HTTP | Message key |
|---|---|---|
| `ORG_DISABLED` | 403 | `errors.org.disabled` |

### question

| Code | HTTP | Message key |
|---|---|---|
| `MODEL_ANSWER_REQUIRED` | 422 | `errors.question.model_answer_required` |
| `QUESTION_INVALID` | 422 | `errors.question.invalid` |

### report

| Code | HTTP | Message key |
|---|---|---|
| `REPORT_FILTER_INVALID` | 422 | `errors.report.filter_invalid` |

### review

| Code | HTTP | Message key |
|---|---|---|
| `DECISION_ALREADY_MADE` | 409 | `errors.review.decision_already_made` |

### role

| Code | HTTP | Message key |
|---|---|---|
| `ROLE_HAS_INVITES` | 409 | `errors.role.has_invites` |
| `ROLE_HAS_NO_ASSESSMENT` | 409 | `errors.role.has_no_assessment` |
| `ROLE_NOT_OPEN` | 409 | `errors.role.not_open` |

### team

| Code | HTTP | Message key |
|---|---|---|
| `LAST_ADMIN_PROTECTED` | 409 | `errors.team.last_admin_protected` |
| `PROTECTED_ACCOUNT` | 403 | `errors.team.protected_account` |

### upload

| Code | HTTP | Message key |
|---|---|---|
| `UPLOAD_INVALID` | 422 | `errors.upload.invalid` |
