"""Engine, sessions and the tenant context (spec 4.1).

Every unit of work runs inside ``tenant_session(ctx)``: one transaction that first sets the
RLS variables (``app.org_id``, ``app.is_platform``) with ``set_config(..., is_local => true)``,
which is the parameterized equivalent of ``SET LOCAL``. Commit on success, rollback on error.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.roles import SYSTEM


@dataclass(frozen=True)
class TenantContext:
    org_id: UUID | None
    user_id: UUID | None
    role: str
    is_platform: bool
    request_id: str
    ip: str | None
    user_agent: str | None
    # Extensions (defaulted, so the frozen positional contract above is unchanged):
    actor_type: str = "user"  # audit actor_type: user | candidate | system
    actor_name: str | None = None  # audit actor_name_snapshot
    session_id: str | None = None  # JWT sid (staff) or invite id (candidate)

    def for_org(self, org_id: UUID | None) -> "TenantContext":
        """Copy scoped to another org (platform code acting inside one org)."""
        return replace(self, org_id=org_id)


def system_context(request_id: str = "system", org_id: UUID | None = None) -> TenantContext:
    """Context for background jobs and CLI: platform mode, audit actor_type ``system``."""
    return TenantContext(
        org_id=org_id,
        user_id=None,
        role=SYSTEM,
        is_platform=True,
        request_id=request_id,
        ip=None,
        user_agent=None,
        actor_type="system",
        actor_name="system",
    )


@lru_cache(maxsize=4)
def _engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def get_engine() -> AsyncEngine:
    return _engine(settings.database_url)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


_ROLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


async def _set_local_role(session: AsyncSession, role: str) -> None:
    """``SET LOCAL ROLE`` so RLS and column grants apply (superusers and BYPASSRLS skip RLS otherwise)."""
    if not role:
        return
    if not _ROLE_RE.fullmatch(role):
        raise ValueError(f"invalid database role name: {role!r}")
    await session.execute(text(f'SET LOCAL ROLE "{role}"'))


async def apply_tenant_settings(session: AsyncSession, ctx: TenantContext) -> None:
    """Set the RLS variables for the current transaction only."""
    await session.execute(
        text("SELECT set_config('app.org_id', :org_id, true), set_config('app.is_platform', :plat, true)"),
        {"org_id": str(ctx.org_id) if ctx.org_id else "", "plat": "on" if ctx.is_platform else "off"},
    )


@asynccontextmanager
async def tenant_session(ctx: TenantContext) -> AsyncIterator[AsyncSession]:
    """One transaction with RLS variables set. Commits on normal exit, rolls back on error."""
    async with get_sessionmaker()() as session:
        async with session.begin():
            await _set_local_role(session, settings.db_app_role)
            await apply_tenant_settings(session, ctx)
            yield session


@asynccontextmanager
async def reports_session(
    ctx: TenantContext, *, statement_timeout_ms: int = 15000
) -> AsyncIterator[AsyncSession]:
    """Read-only transaction on ``DATABASE_URL_REPORTS_RO`` for the report engine (spec 7.4)."""
    url = settings.database_url_reports_ro or settings.database_url
    async with async_sessionmaker(_engine(url), expire_on_commit=False)() as session:
        try:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await _set_local_role(session, settings.db_reports_role)
            await session.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(statement_timeout_ms)}
            )
            await apply_tenant_settings(session, ctx)
            yield session
        finally:
            await session.rollback()  # never commits
