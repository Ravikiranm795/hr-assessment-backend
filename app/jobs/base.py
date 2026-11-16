"""Shared pieces for jobs. Every job is ``async def run(session_factory, now=None) -> dict``:
platform mode (system context), idempotent, one transaction per record so a failure never blocks the rest."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext, system_context, tenant_session

SessionFactory = Callable[[TenantContext], AbstractAsyncContextManager[AsyncSession]]
DEFAULT_FACTORY: SessionFactory = tenant_session
BATCH = 500
log = logging.getLogger("app.jobs")


def now_utc(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def job_ctx(name: str, org_id: UUID | None = None) -> TenantContext:
    return system_context(request_id=f"job-{name}", org_id=org_id)
