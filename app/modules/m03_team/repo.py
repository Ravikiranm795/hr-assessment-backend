"""M3 data access. The only module code that touches the database for user administration.

Every query takes an explicit ``org_id`` (RLS is the second layer). The ``*_any_org`` readers are
for platform (SA) callers, which run in platform mode and address a user by id alone.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.roles import ORG_ADMIN
from app.db import OpenRole, RefreshToken, User

SORT_COLUMNS = {
    "name": User.name,
    "email": User.email,
    "created_at": User.created_at,
    "last_login_at": User.last_login_at,
    "status": User.status,
}


async def get_user(session: AsyncSession, org_id: UUID, user_id: UUID, *, for_update: bool = False) -> User | None:
    stmt = select(User).where(User.id == user_id, User.org_id == org_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_any_org(session: AsyncSession, user_id: UUID, *, for_update: bool = False) -> User | None:
    """Platform callers only (the platform-mode RLS context sees every row)."""
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_users(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None,
    status: str | None,
    role: str | None,
    sort: Sequence[tuple[str, str]],
    offset: int,
    limit: int,
) -> tuple[list[User], int]:
    conditions = [User.org_id == org_id]
    if status:
        conditions.append(User.status == status)
    if role:
        conditions.append(User.role == role)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(or_(User.name.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\")))
    total = (await session.execute(select(func.count()).select_from(User).where(*conditions))).scalar_one()
    order = [
        SORT_COLUMNS[field].desc() if direction == "desc" else SORT_COLUMNS[field].asc() for field, direction in sort
    ] or [User.created_at.asc()]
    rows = await session.execute(
        select(User).where(*conditions).order_by(*order, User.id).offset(offset).limit(limit)
    )
    return list(rows.scalars()), total


async def insert_user(session: AsyncSession, **values) -> User:
    """Insert in a savepoint; the global unique email can collide with a row RLS hides (another org)."""
    user = User(**values)
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError as exc:
        if "email" in str(exc.orig):
            raise AppError("CONFLICT", details={"field": "email", "reason": "email_taken"}) from exc
        raise
    return user


async def active_admin_ids(session: AsyncSession, org_id: UUID) -> list[UUID]:
    """Ids of the org's active admins, row-locked so concurrent deactivations serialize."""
    rows = await session.execute(
        select(User.id)
        .where(User.org_id == org_id, User.role == ORG_ADMIN, User.status == "active")
        .with_for_update()
    )
    return [r[0] for r in rows]


async def save_user(session: AsyncSession) -> None:
    """Flush pending user changes in a savepoint (an email change can hit the global unique index)."""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if "email" in str(exc.orig):
            raise AppError("CONFLICT", details={"field": "email", "reason": "email_taken"}) from exc
        raise


async def revoke_refresh_tokens(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


# --- roles owned by a user. Roles belong to M6; until its service exists this is the one narrow
# --- read/reassign path M3 needs (spec 6/M3: owner_inactive marker, reassign_roles_to).


async def owned_roles(session: AsyncSession, org_id: UUID, user_id: UUID) -> list[OpenRole]:
    rows = await session.execute(
        select(OpenRole)
        .where(OpenRole.org_id == org_id, OpenRole.owner_id == user_id, OpenRole.deleted_at.is_(None))
        .order_by(OpenRole.created_at, OpenRole.id)
    )
    return list(rows.scalars())


async def reassign_roles(session: AsyncSession, org_id: UUID, from_user: UUID, to_user: UUID) -> list[UUID]:
    rows = await session.execute(
        update(OpenRole)
        .where(OpenRole.org_id == org_id, OpenRole.owner_id == from_user, OpenRole.deleted_at.is_(None))
        .values(owner_id=to_user)
        .returning(OpenRole.id)
    )
    return [r[0] for r in rows]
