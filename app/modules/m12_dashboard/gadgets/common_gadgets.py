"""Gadgets shared by HR (own roles), org admin (organization) and, where noted, super admin (platform
counts only, never candidate fields)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.modules.m12_dashboard.gadgets.base import LIVE_ROLE, one, query, register_gadget

_FROM = (
    "FROM invites i JOIN open_roles r ON r.id = i.role_id "
    f"LEFT JOIN attempts a ON a.invite_id = i.id WHERE {LIVE_ROLE}"
)


@register_gadget("summary_counts")
async def summary_counts(ctx: TenantContext, session: AsyncSession) -> dict:
    row = await one(
        session, ctx,
        "SELECT count(*) FILTER (WHERE i.status = 'invited') AS invited, "
        "count(*) FILTER (WHERE i.status = 'started') AS in_progress, "
        "count(*) FILTER (WHERE i.status = 'submitted') AS submitted, "
        f"count(*) FILTER (WHERE a.status = 'submitted' AND a.decision = 'waiting') AS waiting {_FROM}",
    )  # fmt: skip
    return {**row, "links": {k: f"/candidates?status={k}" for k in row}}


@register_gadget("needs_attention")
async def needs_attention(ctx: TenantContext, session: AsyncSession) -> dict:
    row = await one(
        session, ctx,
        "SELECT count(*) FILTER (WHERE a.status = 'submitted' AND a.decision = 'waiting') AS unreviewed, "
        "count(*) FILTER (WHERE i.status = 'invited' AND i.expires_at > now() "
        "AND i.expires_at <= now() + interval '24 hours') AS expiring_links, "
        "count(*) FILTER (WHERE a.status = 'submitted' AND a.decision = 'waiting' "
        "AND (a.flag_counts->'severity'->>'serious')::int > 0) AS flagged_attempts, "
        "count(*) FILTER (WHERE a.status = 'submitted' AND a.decision = 'waiting' AND a.purged_at IS NULL "
        "AND a.submitted_at <= now() - make_interval(days => o.retention_days)) AS undecided_past_retention "
        "FROM invites i JOIN open_roles r ON r.id = i.role_id JOIN organizations o ON o.id = i.org_id "
        f"LEFT JOIN attempts a ON a.invite_id = i.id WHERE {LIVE_ROLE}",
    )  # fmt: skip
    help_open = await one(
        session, ctx,
        "SELECT count(*) AS n FROM help_requests h JOIN invites i ON i.id = h.invite_id "
        f"JOIN open_roles r ON r.id = i.role_id WHERE h.status = 'open' AND {LIVE_ROLE}",
    )  # fmt: skip
    return {**row, "pending_help_requests": help_open["n"]}


@register_gadget("open_roles")
async def open_roles(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT r.id, r.title, count(i.id) FILTER (WHERE i.status = 'invited') AS invited, "
        "count(i.id) FILTER (WHERE i.status = 'started') AS in_progress, "
        "count(i.id) FILTER (WHERE i.status = 'submitted') AS submitted "
        "FROM open_roles r LEFT JOIN invites i ON i.role_id = r.id "
        f"WHERE r.status = 'open' AND {LIVE_ROLE} GROUP BY r.id ORDER BY r.created_at DESC LIMIT 50",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("recent_activity")
async def recent_activity(ctx: TenantContext, session: AsyncSession) -> dict:
    mine = " AND actor_user_id = :owner" if ctx.role == "hr_user" else ""
    rows = await query(
        session, ctx,
        "SELECT action, entity_type, entity_id, actor_name_snapshot AS actor, created_at FROM audit_logs "
        f"WHERE org_id = :org{mine} ORDER BY created_at DESC LIMIT 20",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("candidates_per_week")
async def candidates_per_week(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT date_trunc('week', i.created_at)::date AS week, count(*) AS candidates "
        "FROM invites i JOIN open_roles r ON r.id = i.role_id "
        f"WHERE i.created_at >= now() - interval '12 weeks' AND {LIVE_ROLE} GROUP BY 1 ORDER BY 1",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("decision_split")
async def decision_split(ctx: TenantContext, session: AsyncSession) -> dict:
    row = await one(
        session, ctx,
        "SELECT count(*) FILTER (WHERE a.decision = 'approved') AS approved, "
        "count(*) FILTER (WHERE a.decision = 'rejected') AS rejected, "
        "count(*) FILTER (WHERE a.decision = 'waiting') AS waiting "
        "FROM attempts a JOIN invites i ON i.id = a.invite_id JOIN open_roles r ON r.id = i.role_id "
        f"WHERE a.status = 'submitted' AND {LIVE_ROLE}",
    )  # fmt: skip
    return row


@register_gadget("quick_actions")
async def quick_actions(ctx: TenantContext, session: AsyncSession) -> dict:
    return {
        "items": [
            {"key": "create_role", "path": "/roles/new"},
            {"key": "create_assessment", "path": "/assessments/new"},
            {"key": "invite_candidate", "path": "/roles"},
        ]
    }


@register_gadget("expiring_invites")
async def expiring_invites(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT i.id AS invite_id, i.candidate_name, r.title AS role_title, i.expires_at "
        "FROM invites i JOIN open_roles r ON r.id = i.role_id "
        "WHERE i.status = 'invited' AND i.expires_at > now() AND i.expires_at <= now() + interval '72 hours' "
        f"AND {LIVE_ROLE} ORDER BY i.expires_at LIMIT 20",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("avg_score_by_role")
async def avg_score_by_role(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT r.id AS role_id, r.title, round(avg(a.score_percent), 2) AS avg_score_percent, count(*) "
        "AS attempts "
        "FROM attempts a JOIN invites i ON i.id = a.invite_id JOIN open_roles r ON r.id = i.role_id "
        f"WHERE a.status = 'submitted' AND a.score_percent IS NOT NULL AND {LIVE_ROLE} "
        "GROUP BY r.id ORDER BY r.title LIMIT 50",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("avg_completion_time")
async def avg_completion_time(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT r.id AS role_id, r.title, "
        "round((avg(extract(epoch FROM a.submitted_at - a.started_at)) / 60)::numeric, 1) AS avg_minutes, "
        "count(*) AS attempts FROM attempts a JOIN invites i ON i.id = a.invite_id "
        "JOIN open_roles r ON r.id = i.role_id "
        f"WHERE a.status = 'submitted' AND a.started_at IS NOT NULL AND {LIVE_ROLE} "
        "GROUP BY r.id ORDER BY r.title LIMIT 50",
    )  # fmt: skip
    return {"items": rows}


@register_gadget("proctoring_flags")
async def proctoring_flags(ctx: TenantContext, session: AsyncSession) -> dict:
    rows = await query(
        session, ctx,
        "SELECT a.id AS attempt_id, i.candidate_name, r.title AS role_title, a.flag_counts, a.submitted_at "
        "FROM attempts a JOIN invites i ON i.id = a.invite_id JOIN open_roles r ON r.id = i.role_id "
        "WHERE coalesce((a.flag_counts->'severity'->>'serious')::int, 0) "
        "+ coalesce((a.flag_counts->'severity'->>'moderate')::int, 0) > 0 "
        f"AND {LIVE_ROLE} ORDER BY a.created_at DESC LIMIT 20",
    )  # fmt: skip
    return {"items": rows}
