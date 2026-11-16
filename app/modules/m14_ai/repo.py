"""M14 repository: the only DB access of the AI layer. Every query filters ``org_id``."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AiUsageLog, OrgAiSettings


async def get_ai_settings(session: AsyncSession, org_id: UUID) -> OrgAiSettings | None:
    result = await session.execute(select(OrgAiSettings).where(OrgAiSettings.org_id == org_id))
    return result.scalar_one_or_none()


async def count_calls_since(session: AsyncSession, org_id: UUID, since: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AiUsageLog)
        .where(AiUsageLog.org_id == org_id, AiUsageLog.created_at >= since)
    )
    return int(result.scalar_one())


async def add_usage(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID | None,
    feature: str,
    provider: str,
    model: str,
    status: str,
    tokens_in: int | None,
    tokens_out: int | None,
) -> None:
    """Insert one ``ai_usage_log`` row (metadata only; never prompt or output content)."""
    session.add(
        AiUsageLog(
            org_id=org_id,
            user_id=user_id,
            feature=feature,
            provider=provider,
            model=model,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    )
    await session.flush()
