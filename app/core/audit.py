"""Audit service: the single write path for ``audit_logs`` (spec 4.5, Appendix B).

Call ``record(ctx, session, ACTION, entity_type, entity_id, before, after)`` with the same
session (transaction) as the change it describes. Uses SQLAlchemy Core ``text()`` on the table
name so it has no dependency on ORM models.
"""

import json
import re
from typing import Any

from sqlalchemy import text
from uuid6 import uuid7

from app.core import metrics
from app.core.db import TenantContext

# --- Auth
AUTH_LOGIN_SUCCESS = "AUTH_LOGIN_SUCCESS"
AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
AUTH_LOGOUT = "AUTH_LOGOUT"
AUTH_PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"
AUTH_ACCOUNT_LOCKED = "AUTH_ACCOUNT_LOCKED"
AUTH_ACCOUNT_UNLOCKED = "AUTH_ACCOUNT_UNLOCKED"
AUTH_TEMP_PASSWORD_ISSUED = "AUTH_TEMP_PASSWORD_ISSUED"
AUTH_TOTP_ENABLED = "AUTH_TOTP_ENABLED"
AUTH_TOTP_DISABLED = "AUTH_TOTP_DISABLED"
AUTH_SIGNED_OUT_EVERYWHERE = "AUTH_SIGNED_OUT_EVERYWHERE"
AUTH_DEFAULT_SA_LOGIN = "AUTH_DEFAULT_SA_LOGIN"
# --- Platform
ORG_CREATED = "ORG_CREATED"
ORG_UPDATED = "ORG_UPDATED"
ORG_DISABLED = "ORG_DISABLED"
ORG_ENABLED = "ORG_ENABLED"
ORG_ADMIN_CREATED = "ORG_ADMIN_CREATED"
ORG_ADMIN_DEACTIVATED = "ORG_ADMIN_DEACTIVATED"
ORG_ADMIN_REACTIVATED = "ORG_ADMIN_REACTIVATED"
FEATURE_FLAG_CHANGED = "FEATURE_FLAG_CHANGED"
FEATURE_DEFAULTS_CHANGED = "FEATURE_DEFAULTS_CHANGED"
MISUSE_FLAG_RAISED = "MISUSE_FLAG_RAISED"
MISUSE_FLAG_CLEARED = "MISUSE_FLAG_CLEARED"
PLATFORM_BRANDING_UPDATED = "PLATFORM_BRANDING_UPDATED"
# --- Team and settings
USER_CREATED = "USER_CREATED"
USER_UPDATED = "USER_UPDATED"
USER_DEACTIVATED = "USER_DEACTIVATED"
USER_REACTIVATED = "USER_REACTIVATED"
SETTINGS_UPDATED = "SETTINGS_UPDATED"
BRANDING_UPDATED = "BRANDING_UPDATED"
DOCUMENT_CREATED = "DOCUMENT_CREATED"
DOCUMENT_VERSION_CREATED = "DOCUMENT_VERSION_CREATED"
DOCUMENT_PUBLISHED = "DOCUMENT_PUBLISHED"
EMAIL_SETTINGS_UPDATED = "EMAIL_SETTINGS_UPDATED"
EMAIL_TEST_SENT = "EMAIL_TEST_SENT"
AI_SETTINGS_UPDATED = "AI_SETTINGS_UPDATED"
# --- Assessments
ASSESSMENT_CREATED = "ASSESSMENT_CREATED"
ASSESSMENT_UPDATED = "ASSESSMENT_UPDATED"
ASSESSMENT_DUPLICATED = "ASSESSMENT_DUPLICATED"
ASSESSMENT_ARCHIVED = "ASSESSMENT_ARCHIVED"
ASSESSMENT_DELETED = "ASSESSMENT_DELETED"
VERSION_CREATED = "VERSION_CREATED"
VERSION_PUBLISHED = "VERSION_PUBLISHED"
VERSION_PURGED = "VERSION_PURGED"
QUESTION_ADDED = "QUESTION_ADDED"
QUESTION_UPDATED = "QUESTION_UPDATED"
QUESTION_DELETED = "QUESTION_DELETED"
QUESTIONS_REORDERED = "QUESTIONS_REORDERED"
AI_QUESTIONS_GENERATED = "AI_QUESTIONS_GENERATED"
# --- Roles and invites
ROLE_CREATED = "ROLE_CREATED"
ROLE_UPDATED = "ROLE_UPDATED"
ROLE_OPENED = "ROLE_OPENED"
ROLE_CLOSED = "ROLE_CLOSED"
ROLE_REASSIGNED = "ROLE_REASSIGNED"
ROLE_ASSESSMENT_LINKED = "ROLE_ASSESSMENT_LINKED"
ROLE_ASSESSMENT_REPLACED = "ROLE_ASSESSMENT_REPLACED"
ROLE_ASSESSMENT_UNLINKED = "ROLE_ASSESSMENT_UNLINKED"
INVITE_CREATED = "INVITE_CREATED"
INVITE_BULK_CREATED = "INVITE_BULK_CREATED"
INVITE_REGENERATED = "INVITE_REGENERATED"
INVITE_EXTENDED = "INVITE_EXTENDED"
INVITE_REVOKED = "INVITE_REVOKED"
INVITE_BINDING_RESET = "INVITE_BINDING_RESET"
INVITE_EMAIL_SENT = "INVITE_EMAIL_SENT"
ACCOMMODATION_SET = "ACCOMMODATION_SET"
# --- Exam and review
EXAM_STARTED = "EXAM_STARTED"
EXAM_SUBMITTED = "EXAM_SUBMITTED"
HELP_REQUEST_CREATED = "HELP_REQUEST_CREATED"
HELP_REQUEST_APPROVED = "HELP_REQUEST_APPROVED"
HELP_REQUEST_DECLINED = "HELP_REQUEST_DECLINED"
ATTEMPT_VIEWED = "ATTEMPT_VIEWED"
SNAPSHOT_VIEWED = "SNAPSHOT_VIEWED"
SCORE_OVERRIDDEN = "SCORE_OVERRIDDEN"
AI_SCORING_RUN = "AI_SCORING_RUN"
FLAG_DISMISSED = "FLAG_DISMISSED"
EXTRA_TIME_GRANTED = "EXTRA_TIME_GRANTED"
DECISION_MADE = "DECISION_MADE"
DECISION_OVERRIDDEN = "DECISION_OVERRIDDEN"
CANDIDATE_DATA_DELETED = "CANDIDATE_DATA_DELETED"
# --- Reports and audit
REPORT_INTERPRETED = "REPORT_INTERPRETED"
REPORT_GENERATED = "REPORT_GENERATED"
REPORT_DOWNLOADED = "REPORT_DOWNLOADED"
AUDIT_EXPORTED = "AUDIT_EXPORTED"
# --- System
RETENTION_PURGE_RUN = "RETENTION_PURGE_RUN"
AUDIT_PURGE_RUN = "AUDIT_PURGE_RUN"
VERSION_PURGE_RUN = "VERSION_PURGE_RUN"

AUDIT_ACTIONS: frozenset[str] = frozenset(
    value
    for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, str) and name == value
)

RESULTS = frozenset({"success", "failure", "denied"})
ACTOR_TYPES = frozenset({"user", "candidate", "system"})

REDACTED = "[REDACTED]"
REDACTED_CHANGED_KEY = "_redacted_changed"  # list of redacted top-level fields whose value changed
_REDACT_PATTERN = re.compile(r"^(password.*|.*_hash|.*_enc|token.*|api_key.*|secret.*)$", re.IGNORECASE)
# Extra names that do not match the patterns but must never be stored (spec: "or in the redaction list").
REDACT_EXTRA: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "current_password",
        "new_password",
        "old_password",
        "temp_password",
        "temporary_password",
        "smtp_password",
        "recovery_codes",
        "totp_code",
        "authorization",
        "cookie",
    }
)


def is_sensitive(key: str) -> bool:
    return bool(_REDACT_PATTERN.match(key)) or key.lower() in REDACT_EXTRA


def redact(value: Any) -> Any:
    """Return a copy with every sensitive key (at any depth) replaced by ``[REDACTED]``."""
    if isinstance(value, dict):
        return {k: (REDACTED if is_sensitive(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _redacted_changes(before: dict | None, after: dict | None) -> list[str]:
    before, after = before or {}, after or {}
    keys = [k for k in dict.fromkeys([*before, *after]) if is_sensitive(str(k))]
    return [str(k) for k in keys if before.get(k) != after.get(k)]


def _to_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


_INSERT = text(
    """
    INSERT INTO audit_logs (
        id, org_id, actor_type, actor_user_id, actor_name_snapshot, actor_role, action,
        entity_type, entity_id, before, after, reason, result, ip, user_agent, request_id, created_at
    ) VALUES (
        :id, :org_id, :actor_type, :actor_user_id, :actor_name_snapshot, :actor_role, :action,
        :entity_type, :entity_id, CAST(:before AS jsonb), CAST(:after AS jsonb), :reason, :result,
        :ip, :user_agent, :request_id, now()
    )
    """
)


async def record(
    ctx: TenantContext,
    session: Any,
    action: str,
    entity_type: str,
    entity_id: Any,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    result: str = "success",
) -> None:
    """Insert one redacted audit row in the caller's transaction (never commits).

    ``entity_id`` is stored as text (it may be a UUID or a key such as a flag name).
    """
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r} (spec Appendix B)")
    if result not in RESULTS:
        raise ValueError(f"audit result must be one of {sorted(RESULTS)}")
    if ctx.actor_type not in ACTOR_TYPES:
        raise ValueError(f"audit actor_type must be one of {sorted(ACTOR_TYPES)}")

    safe_before = redact(before) if before is not None else None
    safe_after = redact(after) if after is not None else None
    changed = _redacted_changes(before, after) if before is not None and after is not None else []
    if changed:
        safe_after = {**(safe_after or {}), REDACTED_CHANGED_KEY: changed}

    await session.execute(
        _INSERT,
        {
            "id": uuid7(),
            "org_id": ctx.org_id,
            "actor_type": ctx.actor_type,
            "actor_user_id": ctx.user_id,
            "actor_name_snapshot": ctx.actor_name,
            "actor_role": ctx.role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": None if entity_id is None else str(entity_id),
            "before": _to_json(safe_before),
            "after": _to_json(safe_after),
            "reason": reason,
            "result": result,
            "ip": ctx.ip,
            "user_agent": ctx.user_agent,
            "request_id": ctx.request_id,
        },
    )
    metrics.inc("audit_events_total", action=action)
    if action == EXAM_STARTED:
        metrics.inc("exam_starts_total")
