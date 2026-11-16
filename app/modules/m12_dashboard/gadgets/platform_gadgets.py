"""Super-admin gadgets: platform counts only, never candidate content."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.modules.m12_dashboard.gadgets.base import one, query, register_gadget


@register_gadget("orgs_overview")
async def orgs_overview(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT o.id, o.name, o.status, "
        "(SELECT count(*) FROM users u WHERE u.org_id = o.id AND u.status = 'active') AS active_users, "
        "(SELECT count(*) FROM open_roles r WHERE r.org_id = o.id AND r.status = 'open' "
        "AND r.deleted_at IS NULL) AS open_roles FROM organizations o ORDER BY o.name LIMIT 100",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("active_misuse_flags")
async def active_misuse_flags(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT m.id, m.org_id, o.name AS org_name, m.comment, m.raised_at FROM misuse_flags m "
        "JOIN organizations o ON o.id = m.org_id WHERE m.cleared_at IS NULL ORDER BY m.raised_at DESC "
        "LIMIT 100",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("platform_usage")
async def platform_usage(ctx: TenantContext, session: AsyncSession) -> dict:
    return await one(
        session, ctx,
        "SELECT (SELECT count(*) FROM ai_usage_log WHERE created_at >= date_trunc('month', now())) "
        "AS ai_calls_this_month, (SELECT count(*) FROM organizations WHERE status = 'active') AS "
        "active_orgs, "
        "(SELECT count(*) FROM users WHERE status = 'active') AS active_users, "
        "(SELECT count(*) FROM attempts WHERE created_at >= date_trunc('month', now())) AS "
        "attempts_this_month",
    )  # fmt: skip
