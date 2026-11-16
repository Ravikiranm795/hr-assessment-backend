"""M7 data access. Every query filters ``org_id`` (RLS is the second layer). Roles are read with
``text()`` SQL (owned by M6)."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db import Attempt, HelpRequest, Invite


async def flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_invites_role_email_hash" in str(exc.orig):
            raise AppError("DUPLICATE_INVITE") from exc
        raise


# --------------------------------------------------------------------------- roles (M6 tables, read only)


async def role_owner(session: AsyncSession, org_id: UUID, role_id: UUID) -> UUID | None:
    row = await session.execute(
        text("SELECT owner_id FROM open_roles WHERE id = :r AND org_id = :o AND deleted_at IS NULL"),
        {"r": role_id, "o": org_id},
    )
    return row.scalar_one_or_none()


async def invite_role_owner(session: AsyncSession, org_id: UUID, invite_id: UUID) -> UUID | None:
    row = await session.execute(
        text(
            "SELECT r.owner_id FROM invites i JOIN open_roles r ON r.id = i.role_id "
            "WHERE i.id = :i AND i.org_id = :o"
        ),
        {"i": invite_id, "o": org_id},
    )
    return row.scalar_one_or_none()


async def help_request_role_owner(session: AsyncSession, org_id: UUID, hr_id: UUID) -> UUID | None:
    row = await session.execute(
        text(
            "SELECT r.owner_id FROM help_requests h JOIN invites i ON i.id = h.invite_id "
            "JOIN open_roles r ON r.id = i.role_id WHERE h.id = :h AND h.org_id = :o"
        ),
        {"h": hr_id, "o": org_id},
    )
    return row.scalar_one_or_none()


async def role_title(session: AsyncSession, org_id: UUID, role_id: UUID) -> str | None:
    row = await session.execute(
        text("SELECT title FROM open_roles WHERE id = :r AND org_id = :o"), {"r": role_id, "o": org_id}
    )
    return row.scalar_one_or_none()


async def default_expiry_hours(session: AsyncSession, org_id: UUID) -> int:
    row = await session.execute(
        text("SELECT invite_default_expiry_hours FROM organizations WHERE id = :o"), {"o": org_id}
    )
    return row.scalar_one_or_none() or 72


# --------------------------------------------------------------------------- invites


async def get(session: AsyncSession, org_id: UUID, invite_id: UUID, *, lock: bool = False) -> Invite | None:
    stmt = select(Invite).where(Invite.id == invite_id, Invite.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def attempt_for(session: AsyncSession, org_id: UUID, invite_id: UUID) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.org_id == org_id, Attempt.invite_id == invite_id)
    return (await session.execute(stmt.order_by(Attempt.created_at.desc()).limit(1))).scalar_one_or_none()


async def email_exists(session: AsyncSession, org_id: UUID, role_id: UUID, email_hash: str) -> bool:
    stmt = select(Invite.id).where(
        Invite.org_id == org_id, Invite.role_id == role_id, Invite.candidate_email_hash == email_hash
    )
    return (await session.execute(stmt.limit(1))).first() is not None


async def search(
    session: AsyncSession,
    org_id: UUID,
    role_id: UUID,
    *,
    status: str | None,
    q: str | None,
    offset: int,
    limit: int,
) -> tuple[list[tuple[Invite, Attempt | None]], int]:
    where: list[Any] = [Invite.org_id == org_id, Invite.role_id == role_id]
    if status:
        where.append(Invite.status == status)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(or_(Invite.candidate_name.ilike(f"%{escaped}%", escape="\\")))
    total = (await session.execute(select(func.count()).select_from(Invite).where(*where))).scalar_one()
    stmt = (
        select(Invite, Attempt)
        .outerjoin(Attempt, Attempt.invite_id == Invite.id)
        .where(*where)
        .order_by(Invite.created_at.desc(), Invite.id)
        .offset(offset)
        .limit(limit)
    )
    return [(i, a) for i, a in (await session.execute(stmt)).all()], total


async def revoke_unstarted(session: AsyncSession, org_id: UUID, role_id: UUID) -> list[UUID]:
    rows = await session.execute(
        text(
            "UPDATE invites SET status = 'revoked', updated_at = now() "
            "WHERE org_id = :o AND role_id = :r AND status = 'invited' RETURNING id"
        ),
        {"o": org_id, "r": role_id},
    )
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- help requests


async def get_help_request(
    session: AsyncSession, org_id: UUID, hr_id: UUID, *, lock: bool = False
) -> HelpRequest | None:
    stmt = select(HelpRequest).where(HelpRequest.id == hr_id, HelpRequest.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_help_requests(
    session: AsyncSession,
    org_id: UUID,
    *,
    owner_id: UUID | None,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    where = (
        "h.org_id = :o"
        + (" AND r.owner_id = :u" if owner_id else "")
        + (" AND h.status = :s" if status else "")
    )
    params: dict[str, Any] = {"o": org_id, "u": owner_id, "s": status, "off": offset, "lim": limit}
    base = (
        "FROM help_requests h JOIN invites i ON i.id = h.invite_id "
        f"JOIN open_roles r ON r.id = i.role_id WHERE {where}"
    )
    total = (await session.execute(text(f"SELECT count(*) {base}"), params)).scalar_one()
    rows = await session.execute(
        text(
            "SELECT h.id, h.invite_id, r.id AS role_id, r.title AS role_title, i.candidate_name, h.reason, "
            "h.note, h.status, h.created_at, h.decided_at, h.decision_note, h.granted_camera_exempt, "
            f"h.granted_extra_minutes {base} ORDER BY (h.status = 'open') DESC, h.created_at DESC, h.id "
            "OFFSET :off LIMIT :lim"
        ),
        params,
    )
    return [dict(r._mapping) for r in rows], total


async def help_request_row(session: AsyncSession, org_id: UUID, hr_id: UUID) -> dict:
    rows, _ = await list_help_requests(session, org_id, owner_id=None, status=None, offset=0, limit=10000)
    return next(r for r in rows if r["id"] == hr_id)
