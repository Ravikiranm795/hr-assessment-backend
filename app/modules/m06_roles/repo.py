"""M6 data access. Every query filters ``org_id`` (RLS is the second layer). Tables of other modules
(users, invites, decisions, assessments) are read with ``text()`` SQL, never through their models."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import OpenRole, RoleAssessmentLink


async def get_role(
    session: AsyncSession, org_id: UUID, role_id: UUID, *, lock: bool = False
) -> OpenRole | None:
    stmt = select(OpenRole).where(
        OpenRole.id == role_id, OpenRole.org_id == org_id, OpenRole.deleted_at.is_(None)
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def owner_id(session: AsyncSession, org_id: UUID, role_id: UUID) -> UUID | None:
    stmt = select(OpenRole.owner_id).where(
        OpenRole.id == role_id, OpenRole.org_id == org_id, OpenRole.deleted_at.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def search(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None,
    status: str | None,
    owner: UUID | None,
    department: str | None,
    sort: list[tuple[str, str]],
    offset: int,
    limit: int,
) -> tuple[list[OpenRole], int]:
    where = [OpenRole.org_id == org_id, OpenRole.deleted_at.is_(None)]
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(OpenRole.title.ilike(f"%{escaped}%", escape="\\"))
    if status:
        where.append(OpenRole.status == status)
    if owner:
        where.append(OpenRole.owner_id == owner)
    if department:
        where.append(func.lower(OpenRole.department) == department.lower())
    cols = {"title": OpenRole.title, "created_at": OpenRole.created_at, "updated_at": OpenRole.updated_at}
    order = [cols[f].asc() if d == "asc" else cols[f].desc() for f, d in sort] + [OpenRole.id]
    total = (await session.execute(select(func.count()).select_from(OpenRole).where(*where))).scalar_one()
    rows = (
        await session.execute(select(OpenRole).where(*where).order_by(*order).offset(offset).limit(limit))
    ).scalars()
    return list(rows), total


# --------------------------------------------------------------------------- other modules' tables (read)


async def owners(session: AsyncSession, org_id: UUID, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = await session.execute(
        text("SELECT id, name, status, role FROM users WHERE org_id = :o AND id = ANY(:ids)"),
        {"o": org_id, "ids": ids},
    )
    return {r.id: {"name": r.name, "inactive": r.status != "active", "role": r.role} for r in rows}


async def has_invites(session: AsyncSession, org_id: UUID, role_id: UUID) -> bool:
    row = await session.execute(
        text("SELECT 1 FROM invites WHERE org_id = :o AND role_id = :r LIMIT 1"), {"o": org_id, "r": role_id}
    )
    return row.first() is not None


async def progress(
    session: AsyncSession, org_id: UUID, role_id: UUID
) -> tuple[dict[str, int], dict[str, int]]:
    p = {"o": org_id, "r": role_id}
    invites = {
        r.status: r.n
        for r in await session.execute(
            text(
                "SELECT status, count(*) AS n FROM invites WHERE org_id = :o AND role_id = :r GROUP BY status"
            ),
            p,
        )
    }
    decisions = {
        r.decision: r.n
        for r in await session.execute(
            text(
                "SELECT d.decision, count(*) AS n FROM decisions d JOIN attempts a ON a.id = d.attempt_id "
                "JOIN invites i ON i.id = a.invite_id "
                "WHERE i.org_id = :o AND i.role_id = :r GROUP BY d.decision"
            ),
            p,
        )
    }
    return invites, decisions


# --------------------------------------------------------------------------- links

_LINK_SQL = """
SELECT l.id, l.assessment_version_id, v.assessment_id, a.title AS assessment_title, v.version_no,
       l.linked_by, l.linked_at, l.unlinked_at, l.unlinked_by
  FROM role_assessment_links l
  JOIN assessment_versions v ON v.id = l.assessment_version_id
  JOIN assessments a ON a.id = v.assessment_id
 WHERE l.org_id = :o AND l.role_id = ANY(:roles) {extra}
 ORDER BY l.linked_at DESC, l.id
"""


async def links(
    session: AsyncSession, org_id: UUID, role_ids: list[UUID], *, active_only: bool
) -> list[dict]:
    sql = _LINK_SQL.format(extra="AND l.unlinked_at IS NULL" if active_only else "")
    rows = await session.execute(text(sql), {"o": org_id, "roles": role_ids})
    return [dict(r._mapping) for r in rows]


async def active_links_by_role(session: AsyncSession, org_id: UUID, role_ids: list[UUID]) -> dict[UUID, UUID]:
    if not role_ids:
        return {}
    rows = await session.execute(
        select(RoleAssessmentLink.role_id, RoleAssessmentLink.assessment_version_id).where(
            RoleAssessmentLink.org_id == org_id,
            RoleAssessmentLink.role_id.in_(role_ids),
            RoleAssessmentLink.unlinked_at.is_(None),
        )
    )
    return {r.role_id: r.assessment_version_id for r in rows}


async def active_link(session: AsyncSession, org_id: UUID, role_id: UUID) -> RoleAssessmentLink | None:
    stmt = select(RoleAssessmentLink).where(
        RoleAssessmentLink.org_id == org_id,
        RoleAssessmentLink.role_id == role_id,
        RoleAssessmentLink.unlinked_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def end_link(session: AsyncSession, link: RoleAssessmentLink, by: UUID | None) -> None:
    await session.execute(
        update(RoleAssessmentLink)
        .where(RoleAssessmentLink.id == link.id)
        .values(unlinked_at=datetime.now(UTC), unlinked_by=by)
    )
    await session.flush()
    await session.refresh(link)
