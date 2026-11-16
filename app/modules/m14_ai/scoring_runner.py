"""AI scoring runner (spec 7.3). Registered on ``m09_review.hooks.ai_scoring_hooks``.

- Scores each AI-mode answer through ``AiService.score_answer`` (answer text only, never personal data).
- Stores ``ai_marks`` and ``ai_reason``; ``final_marks`` follows unless HR already overrode it.
- A missing provider, a disabled ``ai_scoring`` flag, a cap or a provider failure sets ``needs_manual`` and
  notifies the role owner (the call itself is audited as ``AI_SCORING_RUN`` with counts only).
- Called from the submit path (candidate request) it only defers: the attempt stays ``pending`` and the
  ``retry_ai_scoring`` job scores it, so the candidate never waits on a provider. HR rescore and the job run
  it.
"""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import notifications
from app.core.db import TenantContext, system_context
from app.core.errors import AppError
from app.modules.m05_assessments.service import get_published_version
from app.modules.m09_review import hooks, repo
from app.modules.m09_review.scoring import ScoringService, money
from app.modules.m14_ai.service import AiService

log = logging.getLogger("app.ai.scoring")


def _answer_text(answer: Any) -> str:
    if isinstance(answer, dict):
        for name in ("text", "gaps", "blanks"):
            value = answer.get(name)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "\n".join(str(v) for v in value)
        return ""
    return "" if answer is None else str(answer)


def _reference(question: Any) -> str:
    if question.model_answer:
        return question.model_answer
    key = question.answer_key if isinstance(question.answer_key, dict) else {}
    return "; ".join(str(p) for p in key.get("key_points", []))


async def _needs_manual(session: AsyncSession, org_id: UUID, attempt, reason: str) -> None:
    attempt.scoring_status = "needs_manual"
    await session.flush()
    owner = await repo.attempt_owner(session, org_id, attempt.id)
    if owner is not None:
        await notifications.notify(
            session, org_id, owner, "scoring_needs_manual",
            {"attempt_id": str(attempt.id), "reason": reason}, "attempt", attempt.id,
        )  # fmt: skip


async def run_ai_scoring(ctx: TenantContext, session: AsyncSession, attempt_id: UUID) -> None:
    if ctx.actor_type == "candidate":
        return  # deferred to the retry job (see module docstring)
    attempt = await repo.get_attempt(session, ctx.org_id, attempt_id, lock=True)
    if attempt is None or attempt.status != "submitted":
        return
    version = await get_published_version(session, attempt.org_id, attempt.assessment_version_id)
    rows = {a.question_id: a for a in await repo.answers(session, attempt.id)}
    system = system_context(f"ai-scoring-{attempt.id}", attempt.org_id)
    service = AiService(session)
    for q in version.questions:
        row = rows.get(q.id)
        if q.scoring_mode != "ai" or row is None or row.ai_marks is not None or row.manual_marks is not None:
            continue
        text = _answer_text(row.answer)
        if not text.strip():
            continue
        try:
            result = await service.score_answer(
                system, q.prompt_html, _reference(q), float(q.marks), text, entity_id=row.id
            )
        except AppError as exc:
            log.warning("ai scoring failed attempt=%s code=%s", attempt.id, exc.code)
            await ScoringService.recompute(session, attempt.org_id, attempt.id)
            await _needs_manual(session, attempt.org_id, attempt, exc.code)
            return
        row.ai_marks = money(Decimal(str(result.fraction)) * Decimal(q.marks))
        row.ai_reason = result.reason
        row.final_marks, row.scored_by = row.ai_marks, "ai"
        await session.flush()
    await ScoringService.recompute(session, attempt.org_id, attempt.id)


if run_ai_scoring not in hooks.ai_scoring_hooks:
    hooks.ai_scoring_hooks.append(run_ai_scoring)
