"""M5b data access. Every query filters ``org_id`` (RLS is the second layer)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import QuestionBankItem as Item


async def get(session: AsyncSession, org_id: UUID, item_id: UUID) -> Item | None:
    return (
        await session.execute(select(Item).where(Item.org_id == org_id, Item.id == item_id))
    ).scalar_one_or_none()


async def owner_id(session: AsyncSession, org_id: UUID, item_id: UUID) -> UUID | None:
    return (
        await session.execute(select(Item.owner_id).where(Item.org_id == org_id, Item.id == item_id))
    ).scalar_one_or_none()


async def search(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None,
    tag: str | None,
    qtype: str | None,
    sort: list[tuple[str, str]],
    offset: int,
    limit: int,
) -> tuple[list[Item], int]:
    conditions = [Item.org_id == org_id]
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Item.prompt_html.ilike(f"%{escaped}%", escape="\\"))
    if tag:
        conditions.append(Item.tags.contains([tag]))
    if qtype:
        conditions.append(Item.type == qtype)
    columns = {"created_at": Item.created_at, "updated_at": Item.updated_at, "marks": Item.marks}
    order = [columns[f].asc() if d == "asc" else columns[f].desc() for f, d in sort] + [Item.id]
    total = (await session.execute(select(func.count()).select_from(Item).where(*conditions))).scalar_one()
    rows = (
        (await session.execute(select(Item).where(*conditions).order_by(*order).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return list(rows), total
