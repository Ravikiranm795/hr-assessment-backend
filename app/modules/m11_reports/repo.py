"""M11 repository: the only app-role DB access of the report chat. Every query is scoped to the caller's
organization (``org_id`` null for super admin reports); RLS is the second layer."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.db import AiUsageLog, Asset, OrgAiSettings, ReportRequest


def _scope(ctx: TenantContext):
    return ReportRequest.org_id == ctx.org_id if ctx.org_id else ReportRequest.org_id.is_(None)


async def add_request(session: AsyncSession, **values) -> ReportRequest:
    row = ReportRequest(**values)
    session.add(row)
    await session.flush()
    return row


async def get_request(session: AsyncSession, ctx: TenantContext, report_id: UUID) -> ReportRequest | None:
    result = await session.execute(select(ReportRequest).where(ReportRequest.id == report_id, _scope(ctx)))
    return result.scalar_one_or_none()


async def list_requests(
    session: AsyncSession, ctx: TenantContext, *, offset: int, limit: int
) -> tuple[list[ReportRequest], int]:
    count = select(func.count()).select_from(ReportRequest).where(_scope(ctx))
    total = (await session.execute(count)).scalar_one()
    rows = await session.execute(
        select(ReportRequest)
        .where(_scope(ctx))
        .order_by(ReportRequest.created_at.desc(), ReportRequest.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), int(total)


async def get_ai_settings(session: AsyncSession, org_id: UUID) -> OrgAiSettings | None:
    result = await session.execute(select(OrgAiSettings).where(OrgAiSettings.org_id == org_id))
    return result.scalar_one_or_none()


async def count_ai_calls_since(session: AsyncSession, org_id: UUID, since: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AiUsageLog)
        .where(AiUsageLog.org_id == org_id, AiUsageLog.created_at >= since)
    )
    return int(result.scalar_one())


async def add_ai_usage(
    session: AsyncSession, *, org_id: UUID, user_id: UUID | None, provider: str, model: str, status: str
) -> None:
    """Metadata only (spec D-10): never the prompt, the question or the output."""
    session.add(
        AiUsageLog(
            org_id=org_id,
            user_id=user_id,
            feature="report_chat",
            provider=provider,
            model=model,
            status=status,
        )
    )
    await session.flush()


async def add_asset(session: AsyncSession, **values) -> Asset:
    asset = Asset(**values)
    session.add(asset)
    await session.flush()
    return asset


async def get_asset(session: AsyncSession, asset_id: UUID) -> Asset | None:
    return (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one_or_none()


async def delete_asset(session: AsyncSession, asset: Asset) -> None:
    await session.delete(asset)
    await session.flush()
