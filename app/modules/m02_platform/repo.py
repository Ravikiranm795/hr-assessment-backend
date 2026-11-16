"""M2 data access (platform mode: callers are super admins, so org ids are always explicit)."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.roles import ORG_ADMIN
from app.db import (
    AiUsageLog,
    Attempt,
    BrandingTheme,
    FeatureFlag,
    Invite,
    MisuseFlag,
    OpenRole,
    Organization,
    OrgFeatureFlag,
    User,
)

ORG_SORT = {"name": Organization.name, "created_at": Organization.created_at, "status": Organization.status}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# --------------------------------------------------------------------------- organizations


async def get_org(session: AsyncSession, org_id: UUID, *, for_update: bool = False) -> Organization | None:
    stmt = select(Organization).where(Organization.id == org_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_orgs(
    session: AsyncSession,
    *,
    q: str | None,
    status: str | None,
    sort: Sequence[tuple[str, str]],
    offset: int,
    limit: int,
) -> tuple[list[Organization], int]:
    conditions = []
    if status:
        conditions.append(Organization.status == status)
    if q:
        conditions.append(
            or_(
                Organization.name.ilike(_like(q), escape="\\"), Organization.slug.ilike(_like(q), escape="\\")
            )
        )
    total = (
        await session.execute(select(func.count()).select_from(Organization).where(*conditions))
    ).scalar_one()
    order = [ORG_SORT[f].desc() if d == "desc" else ORG_SORT[f].asc() for f, d in sort] or [
        Organization.created_at.desc()
    ]
    rows = await session.execute(
        select(Organization).where(*conditions).order_by(*order, Organization.id).offset(offset).limit(limit)
    )
    return list(rows.scalars()), total


async def slug_exists(session: AsyncSession, slug: str) -> bool:
    return (
        await session.execute(select(Organization.id).where(Organization.slug == slug))
    ).first() is not None


async def insert_org(session: AsyncSession, **values) -> Organization:
    org = Organization(**values)
    try:
        async with session.begin_nested():
            session.add(org)
            await session.flush()
    except IntegrityError as exc:
        if "slug" in str(exc.orig):
            raise AppError("CONFLICT", details={"field": "slug", "reason": "slug_taken"}) from exc
        raise
    return org


async def seed_org_flags(session: AsyncSession, org_id: UUID, updated_by: UUID | None) -> int:
    """One explicit override row per catalog flag, copied from the current default set."""
    result = await session.execute(
        text(
            "INSERT INTO org_feature_flags (org_id, flag_key, enabled, updated_by) "
            "SELECT :org, key, default_enabled, :by FROM feature_flags ON CONFLICT DO NOTHING"
        ),
        {"org": org_id, "by": updated_by},
    )
    return result.rowcount or 0


async def insert_org_theme(
    session: AsyncSession, org_id: UUID, theme: dict[str, str], by: UUID | None
) -> None:
    session.add(
        BrandingTheme(
            scope="org",
            org_id=org_id,
            primary_color=theme["primary"],
            accent_color=theme["accent"],
            background_style=theme["background_style"],
            font_key=theme["font_key"],
            updated_by=by,
        )
    )
    await session.flush()


async def active_org_admin_ids(session: AsyncSession, org_id: UUID) -> list[UUID]:
    rows = await session.execute(
        select(User.id)
        .where(User.org_id == org_id, User.role == ORG_ADMIN, User.status == "active")
        .order_by(User.id)
    )
    return [r[0] for r in rows]


async def overview(session: AsyncSession, org_id: UUID, month_start: datetime) -> dict:
    """Counts only. No candidate names, emails, answers or snapshots are ever selected."""
    users = (
        await session.execute(
            select(User.role, User.status, func.count())
            .where(User.org_id == org_id)
            .group_by(User.role, User.status)
        )
    ).all()
    roles = (
        await session.execute(
            select(OpenRole.status, func.count())
            .where(OpenRole.org_id == org_id, OpenRole.deleted_at.is_(None))
            .group_by(OpenRole.status)
        )
    ).all()
    invites = (
        await session.execute(
            select(Invite.status, func.count()).where(Invite.org_id == org_id).group_by(Invite.status)
        )
    ).all()
    attempts = (
        await session.execute(
            select(Attempt.status, func.count()).where(Attempt.org_id == org_id).group_by(Attempt.status)
        )
    ).all()
    calls = (
        await session.execute(
            select(func.count())
            .select_from(AiUsageLog)
            .where(AiUsageLog.org_id == org_id, AiUsageLog.created_at >= month_start)
        )
    ).scalar_one()
    return {"users": users, "roles": roles, "invites": invites, "attempts": attempts, "ai_calls": calls}


async def list_org_users(
    session: AsyncSession, org_id: UUID, offset: int, limit: int
) -> tuple[list[User], int]:
    total = (
        await session.execute(select(func.count()).select_from(User).where(User.org_id == org_id))
    ).scalar_one()
    rows = await session.execute(
        select(User)
        .where(User.org_id == org_id)
        .order_by(User.role, User.name, User.id)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total


async def list_org_roles(
    session: AsyncSession, org_id: UUID, offset: int, limit: int
) -> tuple[list[dict], int]:
    cond = (OpenRole.org_id == org_id, OpenRole.deleted_at.is_(None))
    total = (await session.execute(select(func.count()).select_from(OpenRole).where(*cond))).scalar_one()
    rows = await session.execute(
        select(
            OpenRole.id,
            OpenRole.title,
            OpenRole.department,
            OpenRole.status,
            OpenRole.owner_id,
            User.name.label("owner_name"),
            (User.status != "active").label("owner_inactive"),
            OpenRole.created_at,
        )
        .join(User, User.id == OpenRole.owner_id)
        .where(*cond)
        .order_by(OpenRole.created_at.desc(), OpenRole.id)
        .offset(offset)
        .limit(limit)
    )
    return [dict(r._mapping) for r in rows], total


# --------------------------------------------------------------------------- feature flags


async def flag_catalog(session: AsyncSession) -> list[FeatureFlag]:
    return list((await session.execute(select(FeatureFlag).order_by(FeatureFlag.key))).scalars())


async def org_flag_raw(session: AsyncSession, org_id: UUID) -> dict[str, bool]:
    """Override if present, else the catalog default (before dependency resolution)."""
    rows = await session.execute(
        select(FeatureFlag.key, func.coalesce(OrgFeatureFlag.enabled, FeatureFlag.default_enabled)).outerjoin(
            OrgFeatureFlag, (OrgFeatureFlag.flag_key == FeatureFlag.key) & (OrgFeatureFlag.org_id == org_id)
        )
    )
    return {k: bool(v) for k, v in rows}


async def set_org_flag(session: AsyncSession, org_id: UUID, key: str, enabled: bool, by: UUID | None) -> None:
    stmt = pg_insert(OrgFeatureFlag).values(org_id=org_id, flag_key=key, enabled=enabled, updated_by=by)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["org_id", "flag_key"],
            set_={"enabled": enabled, "updated_by": by, "updated_at": func.now()},
        )
    )


async def freeze_existing_orgs(session: AsyncSession) -> None:
    """Give every org an explicit row per flag so a default change never reaches existing orgs."""
    await session.execute(
        text(
            "INSERT INTO org_feature_flags (org_id, flag_key, enabled) "
            "SELECT o.id, f.key, f.default_enabled FROM organizations o "
            "CROSS JOIN feature_flags f ON CONFLICT DO NOTHING"
        )
    )


async def set_default(session: AsyncSession, key: str, enabled: bool) -> None:
    await session.execute(
        update(FeatureFlag)
        .where(FeatureFlag.key == key)
        .values(default_enabled=enabled, updated_at=func.now())
    )


# --------------------------------------------------------------------------- misuse flags


async def get_misuse_flag(session: AsyncSession, flag_id: UUID) -> MisuseFlag | None:
    return (
        await session.execute(select(MisuseFlag).where(MisuseFlag.id == flag_id).with_for_update())
    ).scalar_one_or_none()


async def list_misuse_flags(
    session: AsyncSession, org_id: UUID, status: str | None, offset: int, limit: int
) -> tuple[list[MisuseFlag], int]:
    cond = [MisuseFlag.org_id == org_id]
    if status:
        cond.append(MisuseFlag.status == status)
    total = (await session.execute(select(func.count()).select_from(MisuseFlag).where(*cond))).scalar_one()
    rows = await session.execute(
        select(MisuseFlag)
        .where(*cond)
        .order_by(MisuseFlag.raised_at.desc(), MisuseFlag.id)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total


# --------------------------------------------------------------------------- platform theme


async def get_platform_theme(session: AsyncSession) -> BrandingTheme | None:
    return (
        await session.execute(select(BrandingTheme).where(BrandingTheme.scope == "platform"))
    ).scalar_one_or_none()


async def save_platform_theme(session: AsyncSession, values: dict, by: UUID | None) -> BrandingTheme:
    theme = await get_platform_theme(session)
    if theme is None:
        theme = BrandingTheme(scope="platform", org_id=None, updated_by=by, **values)
        session.add(theme)
    else:
        for key, value in values.items():
            setattr(theme, key, value)
        theme.updated_by = by
    await session.flush()
    return theme


# --------------------------------------------------------------------------- usage


async def usage(
    session: AsyncSession, start: datetime, end: datetime, offset: int, limit: int
) -> tuple[list[dict], int]:
    total = (await session.execute(select(func.count()).select_from(Organization))).scalar_one()
    in_month = (AiUsageLog.created_at >= start) & (AiUsageLog.created_at < end)
    rows = await session.execute(
        select(
            Organization.id,
            Organization.name,
            func.count(AiUsageLog.id).label("calls"),
            func.coalesce(func.sum(AiUsageLog.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(AiUsageLog.tokens_out), 0).label("tokens_out"),
        )
        .outerjoin(AiUsageLog, (AiUsageLog.org_id == Organization.id) & in_month)
        .group_by(Organization.id, Organization.name)
        .order_by(Organization.name, Organization.id)
        .offset(offset)
        .limit(limit)
    )
    items = [dict(r._mapping) for r in rows]
    if items:
        feats = await session.execute(
            select(AiUsageLog.org_id, AiUsageLog.feature, func.count())
            .where(in_month, AiUsageLog.org_id.in_([i["id"] for i in items]))
            .group_by(AiUsageLog.org_id, AiUsageLog.feature)
        )
        by_org: dict[UUID, dict[str, int]] = {}
        for org_id, feature, n in feats:
            by_org.setdefault(org_id, {})[feature] = n
        for item in items:
            item["by_feature"] = by_org.get(item["id"], {})
    return items, total
