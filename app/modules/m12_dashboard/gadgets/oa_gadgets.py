"""Organization-admin-only gadgets."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.modules.m12_dashboard.gadgets.base import query, register_gadget

SECURITY_ACTIONS = (
    "AUTH_LOGIN_FAILED", "AUTH_ACCOUNT_LOCKED", "AUTH_ACCOUNT_UNLOCKED", "AUTH_TEMP_PASSWORD_ISSUED",
    "AUTH_TOTP_DISABLED", "AUTH_SIGNED_OUT_EVERYWHERE",
)  # fmt: skip


@register_gadget("roles_by_recruiter")
async def roles_by_recruiter(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT u.id AS user_id, u.name, count(r.id) FILTER (WHERE r.status = 'open') AS open_roles, "
        "count(r.id) AS total_roles FROM users u LEFT JOIN open_roles r ON r.owner_id = u.id "
        "AND r.deleted_at IS NULL WHERE u.org_id = :org AND u.role IN ('hr_user', 'org_admin') "
        "GROUP BY u.id ORDER BY open_roles DESC, u.name LIMIT 50",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("security_events")
async def security_events(ctx: TenantContext, session: AsyncSession) -> dict:
    counts = await query(
        session, ctx,
        "SELECT action, count(*) AS n FROM audit_logs WHERE org_id = :org AND action = ANY(:actions) "
        "AND created_at >= now() - interval '7 days' GROUP BY action ORDER BY action",
        actions=list(SECURITY_ACTIONS),
    )  # fmt: skip
    latest = await query(
        session, ctx,
        "SELECT action, actor_name_snapshot AS actor, result, created_at FROM audit_logs "
        "WHERE org_id = :org AND action = ANY(:actions) ORDER BY created_at DESC LIMIT 20",
        actions=list(SECURITY_ACTIONS),
    )  # fmt: skip
    return {"last_7_days": {c["action"]: c["n"] for c in counts}, "latest": latest}


@register_gadget("active_flag_notice")
async def active_flag_notice(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT id, comment, raised_at FROM misuse_flags WHERE org_id = :org AND cleared_at IS NULL "
        "ORDER BY raised_at DESC",
    )  # fmt: skip
    return {"items": rows}
