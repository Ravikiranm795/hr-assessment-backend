"""Registry run by ``RoleService.close`` inside the closing transaction.

A hook receives ``(ctx, session, role_id)``; M7 registers one that revokes unstarted invites, so the
close and the revocations commit or roll back together. Register at import time of the owning module.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext

RoleCloseHook = Callable[[TenantContext, AsyncSession, UUID], Awaitable[None]]

on_role_close: list[RoleCloseHook] = []
