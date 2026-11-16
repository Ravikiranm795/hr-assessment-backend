"""Database access for M1. The only module code that touches the tables.

Pre-authentication lookups (``get_user_by_email``, ``get_user_any_org``, refresh tokens by hash)
cannot know the organization yet: they run in platform mode on purpose and the service scopes
everything it does afterwards. Every authenticated lookup filters by ``org_id``.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import ORG_ADMIN
from app.db import Organization, PasswordHistory, RefreshToken, User


def _org_filter(org_id: UUID | None):
    return User.org_id.is_(None) if org_id is None else User.org_id == org_id


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Login lookup across organizations (email is globally unique, citext)."""
    return (await session.execute(select(User).where(User.email == email.strip()))).scalar_one_or_none()


async def get_user_any_org(session: AsyncSession, user_id: UUID) -> User | None:
    """Lookup by id for flows that start from an opaque token (refresh, TOTP challenge)."""
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_user(session: AsyncSession, org_id: UUID | None, user_id: UUID) -> User | None:
    return (
        await session.execute(select(User).where(User.id == user_id, _org_filter(org_id)))
    ).scalar_one_or_none()


async def get_org(session: AsyncSession, org_id: UUID) -> Organization | None:
    return (await session.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()


async def active_org_admin_ids(session: AsyncSession, org_id: UUID) -> list[UUID]:
    rows = await session.execute(
        select(User.id).where(User.org_id == org_id, User.role == ORG_ADMIN, User.status == "active")
    )
    return list(rows.scalars())


# --------------------------------------------------------------------------- refresh tokens


async def add_refresh_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    family_id: UUID,
    token_hash: str,
    expires_at: datetime,
    ip: str | None,
    user_agent: str | None,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user_id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip=ip,
        user_agent=user_agent,
    )
    session.add(token)
    await session.flush()
    return token


async def get_refresh_token_for_update(session: AsyncSession, token_hash: str) -> RefreshToken | None:
    """Row lock, so two concurrent refreshes of one token serialize (the second is a reuse)."""
    return (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
    ).scalar_one_or_none()


async def revoke_family(
    session: AsyncSession, family_id: UUID, now: datetime, user_id: UUID | None = None
) -> int:
    stmt = update(RefreshToken).where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
    if user_id is not None:
        stmt = stmt.where(RefreshToken.user_id == user_id)
    return (await session.execute(stmt.values(revoked_at=now))).rowcount


async def revoke_all_for_user(session: AsyncSession, user_id: UUID, now: datetime) -> int:
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return (await session.execute(stmt)).rowcount


# --------------------------------------------------------------------------- password history


async def recent_password_hashes(session: AsyncSession, user_id: UUID, limit: int) -> list[str]:
    rows = await session.execute(
        select(PasswordHistory.password_hash)
        .where(PasswordHistory.user_id == user_id)
        .order_by(PasswordHistory.created_at.desc(), PasswordHistory.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def add_password_history(
    session: AsyncSession, user_id: UUID, password_hash: str, created_at: datetime
) -> None:
    """Record a hash. ``created_at`` is explicit so two inserts in one transaction keep their order."""
    session.add(PasswordHistory(user_id=user_id, password_hash=password_hash, created_at=created_at))
    await session.flush()


async def prune_password_history(session: AsyncSession, user_id: UUID, keep: int) -> None:
    """Keep only the newest ``keep`` entries for the user."""
    stale = (
        select(PasswordHistory.id)
        .where(PasswordHistory.user_id == user_id)
        .order_by(PasswordHistory.created_at.desc(), PasswordHistory.id.desc())
        .offset(keep)
    )
    ids = list((await session.execute(stale)).scalars())
    if ids:
        await session.execute(PasswordHistory.__table__.delete().where(PasswordHistory.id.in_(ids)))
