"""Registry run by ``ExamService.finalize`` inside the submitting transaction.

A hook receives ``(ctx, session, attempt_id)``; M9 registers the scoring starter here.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext

SubmitHook = Callable[[TenantContext, AsyncSession, UUID], Awaitable[None]]

on_submit: list[SubmitHook] = []
