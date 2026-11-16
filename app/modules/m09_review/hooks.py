"""Registry for AI scoring. ``ScoringService.score_attempt`` and the retry job call every hook with
``(ctx, session, attempt_id)`` for attempts that have AI-scored answers; M19 registers its runner and
calls ``ScoringService.recompute`` itself after writing marks."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext

AiScoringHook = Callable[[TenantContext, AsyncSession, UUID], Awaitable[None]]

ai_scoring_hooks: list[AiScoringHook] = []
