"""Gadget registry and scoping. A gadget is ``async def data(ctx, session) -> dict``."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext

Gadget = Callable[[TenantContext, AsyncSession], Awaitable[dict[str, Any]]]
GADGETS: dict[str, Gadget] = {}

# Roles are scoped by org (none = platform) and owner (HR sees only their own roles).
ROLE_SCOPE = (
    "(CAST(:org AS uuid) IS NULL OR r.org_id = :org) "
    "AND (CAST(:owner AS uuid) IS NULL OR r.owner_id = :owner)"
)
LIVE_ROLE = f"r.deleted_at IS NULL AND {ROLE_SCOPE}"


def register_gadget(key: str) -> Callable[[Gadget], Gadget]:
    def decorator(fn: Gadget) -> Gadget:
        if key in GADGETS:
            raise ValueError(f"gadget {key!r} registered twice")
        GADGETS[key] = fn
        return fn

    return decorator


def params(ctx: TenantContext, **extra: Any) -> dict[str, Any]:
    return {"org": ctx.org_id, "owner": ctx.user_id if ctx.role == "hr_user" else None, **extra}


async def query(session: AsyncSession, ctx: TenantContext, sql: str, **extra: Any) -> list[dict[str, Any]]:
    result = await session.execute(text(sql), params(ctx, **extra))
    return [dict(r._mapping) for r in result]


async def one(session: AsyncSession, ctx: TenantContext, sql: str, **extra: Any) -> dict[str, Any]:
    return (await query(session, ctx, sql, **extra))[0]
