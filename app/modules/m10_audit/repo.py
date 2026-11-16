"""Audit reads (the only DB access of M10). The application role has SELECT only on ``audit_logs``.

``audit_logs`` is partitioned monthly on ``created_at``: every statement here carries a
``created_at`` range so Postgres prunes partitions (the list window, or a window derived from the
UUIDv7 timestamp for a single row). ``org_id`` is filtered when given; ``None`` means the
platform (super admin) view, which RLS only allows in platform mode.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AuditLog

RETENTION_FLOOR = timedelta(days=31 * 14)  # 13-month retention plus a buffer; bounds lookups of non-v7 ids
ID_TIME_SLACK = timedelta(days=2)

_SORT_COLUMNS = {
    "created_at": AuditLog.created_at,
    "action": AuditLog.action,
    "actor_name": AuditLog.actor_name_snapshot,
    "result": AuditLog.result,
}
SORT_FIELDS = frozenset(_SORT_COLUMNS)


@dataclass(frozen=True)
class Window:
    date_from: datetime
    date_to: datetime | None = None


@dataclass(frozen=True)
class Criteria:
    window: Window
    actor_user_id: UUID | None = None
    actor_type: str | None = None
    actions: tuple[str, ...] = field(default_factory=tuple)
    entity_type: str | None = None
    entity_id: str | None = None
    result: str | None = None
    q: str | None = None


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _conditions(org_id: UUID | None, c: Criteria) -> list:
    conds = [AuditLog.created_at >= c.window.date_from]
    if c.window.date_to is not None:
        conds.append(AuditLog.created_at <= c.window.date_to)
    if org_id is not None:
        conds.append(AuditLog.org_id == org_id)
    if c.actor_user_id:
        conds.append(AuditLog.actor_user_id == c.actor_user_id)
    if c.actor_type:
        conds.append(AuditLog.actor_type == c.actor_type)
    if c.actions:
        conds.append(AuditLog.action.in_(c.actions))
    if c.entity_type:
        conds.append(AuditLog.entity_type == c.entity_type)
    if c.entity_id:
        conds.append(AuditLog.entity_id == c.entity_id)
    if c.result:
        conds.append(AuditLog.result == c.result)
    if c.q:
        pattern = _like(c.q)
        conds.append(
            or_(
                *(
                    col.ilike(pattern, escape="\\")
                    for col in (
                        AuditLog.actor_name_snapshot,
                        AuditLog.action,
                        AuditLog.entity_type,
                        AuditLog.entity_id,
                        AuditLog.reason,
                    )
                )
            )
        )
    return conds


def search_stmt(
    org_id: UUID | None, criteria: Criteria, sort: list[tuple[str, str]], limit: int | None, offset: int
) -> Select:
    stmt = select(AuditLog).where(and_(*_conditions(org_id, criteria)))
    order = [
        _SORT_COLUMNS[name].asc() if direction == "asc" else _SORT_COLUMNS[name].desc()
        for name, direction in sort
    ]
    # Stable paging: newest first among equal keys (UUIDv7 ids sort by time).
    stmt = stmt.order_by(*order, AuditLog.created_at.desc(), AuditLog.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    return stmt


async def fetch(
    session: AsyncSession,
    org_id: UUID | None,
    criteria: Criteria,
    sort: list[tuple[str, str]],
    limit: int | None,
    offset: int = 0,
) -> list[AuditLog]:
    return list((await session.execute(search_stmt(org_id, criteria, sort, limit, offset))).scalars())


async def search(
    session: AsyncSession,
    org_id: UUID | None,
    criteria: Criteria,
    sort: list[tuple[str, str]],
    limit: int | None,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    rows = await fetch(session, org_id, criteria, sort, limit, offset)
    total = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(and_(*_conditions(org_id, criteria)))
        )
    ).scalar_one()
    return rows, total


async def count_by_action(session: AsyncSession, org_id: UUID | None, criteria: Criteria) -> dict[str, int]:
    stmt = (
        select(AuditLog.action, func.count())
        .where(and_(*_conditions(org_id, criteria)))
        .group_by(AuditLog.action)
    )
    return {action: n for action, n in (await session.execute(stmt)).all()}


def _id_window(log_id: UUID, now: datetime) -> tuple[datetime, datetime]:
    """created_at bounds for one id. Ids are UUIDv7 (time in the first 48 bits); other versions
    fall back to the retention window."""
    if log_id.version == 7:
        created = datetime.fromtimestamp((log_id.int >> 80) / 1000, tz=UTC)
        return created - ID_TIME_SLACK, created + ID_TIME_SLACK
    return now - RETENTION_FLOOR, now + ID_TIME_SLACK


def get_stmt(org_id: UUID | None, log_id: UUID, now: datetime | None = None) -> Select:
    low, high = _id_window(log_id, now or datetime.now(UTC))
    conds = [AuditLog.id == log_id, AuditLog.created_at >= low, AuditLog.created_at <= high]
    if org_id is not None:
        conds.append(AuditLog.org_id == org_id)
    return select(AuditLog).where(and_(*conds))


async def get(session: AsyncSession, org_id: UUID | None, log_id: UUID) -> AuditLog | None:
    return (await session.execute(get_stmt(org_id, log_id))).scalars().first()
