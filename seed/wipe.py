"""Remove demo data: the demo organizations (flagged ``is_demo``, reserved slug, created by the demo
platform admin) with everything inside them, plus the demo platform admin. Real data is never touched.

The audit log is append-only (a database trigger refuses deletes), so audit rows written for demo
organizations stay; they only reference ids that no longer exist.
"""

from sqlalchemy import text

from app.core.config import settings
from app.core.db import system_context, tenant_session
from app.core.storage import get_storage
from seed.data import ORG1_SLUG, ORG2_SLUG, PLATFORM_ADMIN

# A demo organization is flagged ``is_demo``, carries one of the reserved demo slugs AND was created by the
# demo platform admin. All three must hold, so no other organization can ever be matched.
DEMO_ORGS_SQL = (
    "SELECT id FROM organizations WHERE is_demo AND slug IN (:s1, :s2) AND created_by IN "
    "(SELECT id FROM users WHERE org_id IS NULL AND email = :sa AND NOT is_default_super_admin)"
)
DEMO_PARAMS = {"s1": ORG1_SLUG, "s2": ORG2_SLUG, "sa": PLATFORM_ADMIN.email}


def refuse_in_prod() -> None:
    """Hard stop for every seed entry point (the CLI check only adds a friendlier message)."""
    if settings.app_env == "prod":
        raise RuntimeError("Demo data is never created or removed in a production environment (APP_ENV=prod)")


# Child tables first. Each statement is scoped by the demo organization ids (:o).
_DELETES = (
    "DELETE FROM proctor_events WHERE org_id = ANY(:o)",
    "DELETE FROM help_requests WHERE org_id = ANY(:o)",
    "DELETE FROM decisions WHERE org_id = ANY(:o)",
    "DELETE FROM attempt_answers WHERE attempt_id IN (SELECT id FROM attempts WHERE org_id = ANY(:o))",
    "DELETE FROM attempts WHERE org_id = ANY(:o)",
    "DELETE FROM invites WHERE org_id = ANY(:o)",
    "DELETE FROM role_assessment_links WHERE org_id = ANY(:o)",
    "DELETE FROM open_roles WHERE org_id = ANY(:o)",
    "DELETE FROM ai_usage_log WHERE org_id = ANY(:o)",
    "DELETE FROM report_requests WHERE org_id = ANY(:o)",
    "DELETE FROM misuse_flags WHERE org_id = ANY(:o)",
    "DELETE FROM question_bank_items WHERE org_id = ANY(:o)",
    # versions cascade to questions and options (the database allows a whole-version delete)
    "DELETE FROM assessment_versions WHERE org_id = ANY(:o)",
    "DELETE FROM assessments WHERE org_id = ANY(:o)",
    "DELETE FROM documents WHERE org_id = ANY(:o)",  # cascades to document_versions
    "DELETE FROM notifications WHERE org_id = ANY(:o)",
    "DELETE FROM branding_themes WHERE org_id = ANY(:o)",
    "DELETE FROM org_ai_settings WHERE org_id = ANY(:o)",
    "DELETE FROM org_email_settings WHERE org_id = ANY(:o)",
    "DELETE FROM org_feature_flags WHERE org_id = ANY(:o)",
    "DELETE FROM users WHERE org_id = ANY(:o)",  # cascades to gadgets, tokens, password history
    "DELETE FROM assets WHERE org_id = ANY(:o)",
    "DELETE FROM organizations WHERE id = ANY(:o)",
)


async def wipe_demo() -> dict[str, int]:
    """Delete all demo data in one transaction. Returns ``{"organizations": n, "storage_files": n}``."""
    refuse_in_prod()
    ctx = system_context("reset-demo")
    async with tenant_session(ctx) as session:
        org_ids = list((await session.execute(text(DEMO_ORGS_SQL), DEMO_PARAMS)).scalars())
        keys: list[str] = []
        if org_ids:
            keys = list(
                (
                    await session.execute(
                        text("SELECT storage_key FROM assets WHERE org_id = ANY(:o)"), {"o": org_ids}
                    )
                ).scalars()
            )
            for statement in _DELETES:
                await session.execute(text(statement), {"o": org_ids})
        await session.execute(
            text("DELETE FROM users WHERE org_id IS NULL AND email = :e AND NOT is_default_super_admin"),
            {"e": PLATFORM_ADMIN.email},
        )
    storage = get_storage()
    for key in keys:
        await storage.delete(key)  # only after the transaction committed
    return {"organizations": len(org_ids), "storage_files": len(keys)}
