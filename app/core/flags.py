"""Feature flag service (spec 4.4, Appendix E). Reads ``feature_flags`` + ``org_feature_flags``
with SQLAlchemy Core ``text()`` (no ORM models)."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

DASHBOARD = "dashboard"
DASHBOARD_GADGETS = "dashboard_gadgets"
ASSESSMENTS = "assessments"
AI_QUESTION_BUILDER = "ai_question_builder"
AI_SCORING = "ai_scoring"
QUESTION_BANK = "question_bank"
ROLES = "roles"
INVITES = "invites"
CSV_BULK_INVITE = "csv_bulk_invite"
EMAIL_SENDING = "email_sending"
HELP_REQUESTS = "help_requests"
CAMERA_PROCTORING = "camera_proctoring"
REVIEW = "review"
REPORTS_CHAT = "reports_chat"
AUDIT_VIEW = "audit_view"
AUDIT_EXPORT = "audit_export"
TEAM = "team"
SETTINGS = "settings"
ORG_BRANDING = "org_branding"
POLICY_DOCUMENTS = "policy_documents"
AI_PROVIDER_SETUP = "ai_provider_setup"
WATERMARK = "watermark"

FLAG_KEYS: tuple[str, ...] = (
    DASHBOARD,
    DASHBOARD_GADGETS,
    ASSESSMENTS,
    AI_QUESTION_BUILDER,
    AI_SCORING,
    QUESTION_BANK,
    ROLES,
    INVITES,
    CSV_BULK_INVITE,
    EMAIL_SENDING,
    HELP_REQUESTS,
    CAMERA_PROCTORING,
    REVIEW,
    REPORTS_CHAT,
    AUDIT_VIEW,
    AUDIT_EXPORT,
    TEAM,
    SETTINGS,
    ORG_BRANDING,
    POLICY_DOCUMENTS,
    AI_PROVIDER_SETUP,
    WATERMARK,
)

# A flag is effective only if all flags it depends on are effective (Appendix E: "invites needs
# roles and assessments"; bulk invite is part of invites).
FLAG_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    INVITES: (ROLES, ASSESSMENTS),
    CSV_BULK_INVITE: (INVITES,),
}

_QUERY = text(
    """
    SELECT f.key AS key,
           COALESCE(o.enabled, f.default_enabled) AS enabled,
           f.visible_to_org AS visible_to_org
    FROM feature_flags f
    LEFT JOIN org_feature_flags o ON o.flag_key = f.key AND o.org_id = :org_id
    """
)


def _resolve(raw: dict[str, bool], key: str, seen: frozenset[str] = frozenset()) -> bool:
    if not raw.get(key, False) or key in seen:
        return False
    return all(_resolve(raw, dep, seen | {key}) for dep in FLAG_DEPENDENCIES.get(key, ()))


async def effective_map(
    session: Any, org_id: UUID | None, *, include_hidden: bool = False
) -> dict[str, bool]:
    """Effective flag values for an org (override, else catalog default, then dependencies).
    ``include_hidden=False`` omits flags with ``visible_to_org = false`` (use for non-SA users).
    ``org_id=None`` returns catalog defaults."""
    rows = (await session.execute(_QUERY, {"org_id": org_id})).mappings().all()
    raw = {row["key"]: bool(row["enabled"]) for row in rows}
    visible = {row["key"]: bool(row["visible_to_org"]) for row in rows}
    return {key: _resolve(raw, key) for key in raw if include_hidden or visible[key]}


async def is_enabled(session: Any, org_id: UUID | None, key: str) -> bool:
    """Effective value of one flag; unknown keys are ``False``."""
    return (await effective_map(session, org_id, include_hidden=True)).get(key, False)
