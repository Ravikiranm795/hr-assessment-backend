"""HR-only gadgets."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.modules.m12_dashboard.gadgets.base import one, register_gadget


@register_gadget("my_assessments")
async def my_assessments(ctx: TenantContext, session: AsyncSession) -> dict:
    return await one(
        session, ctx,
        "SELECT count(DISTINCT a.id) AS assessments, "
        "count(v.id) FILTER (WHERE v.status = 'draft') AS draft_versions, "
        "count(v.id) FILTER (WHERE v.status = 'published') AS published_versions, "
        "(SELECT count(DISTINCT l.assessment_version_id) FROM role_assessment_links l "
        "JOIN open_roles r ON r.id = l.role_id JOIN assessment_versions lv ON lv.id = "
        "l.assessment_version_id "
        "JOIN assessments la ON la.id = lv.assessment_id WHERE l.unlinked_at IS NULL AND r.status <> "
        "'closed' "
        "AND la.owner_id = :owner AND la.deleted_at IS NULL) AS versions_in_use "
        "FROM assessments a LEFT JOIN assessment_versions v ON v.assessment_id = a.id "
        "WHERE a.owner_id = :owner AND a.deleted_at IS NULL AND a.org_id = :org",
    )  # fmt: skip
