"""Scoring (spec 7.3 auto part). Runs when M8 submits; AI-scored answers go to ``ai_scoring_hooks``."""

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext
from app.core.errors import AppError
from app.db import AttemptAnswer
from app.modules.m05_assessments.service import PublishedVersion, get_published_version, grade_auto
from app.modules.m08_exam import hooks as exam_hooks
from app.modules.m09_review import hooks, repo

TWO = Decimal("0.01")


def money(value: Decimal | int | float) -> Decimal:
    return Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP)


def _has_text(answer) -> bool:
    return answer is not None and bool(str(answer).strip())


class ScoringService:
    @staticmethod
    async def score_attempt(ctx: TenantContext, session: AsyncSession, attempt_id: UUID) -> None:
        """Auto marks first, then the AI hooks for AI-scored answers. Unanswered questions score 0."""
        attempt = await repo.get_attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt is None:
            raise AppError("NOT_FOUND")
        version = await get_published_version(session, attempt.org_id, attempt.assessment_version_id)
        existing = {a.question_id: a for a in await repo.answers(session, attempt.id)}
        has_ai = False
        for q in version.questions:
            row = existing.get(q.id)
            if row is None:
                row = AttemptAnswer(attempt_id=attempt.id, question_id=q.id, answer=None)
                session.add(row)
            if q.scoring_mode == "auto":
                row.auto_marks = row.final_marks = grade_auto(q, row.answer)
                row.scored_by = "auto"
            elif not _has_text(row.answer):
                row.final_marks, row.scored_by = Decimal("0.00"), "auto"
            elif q.scoring_mode == "ai":
                has_ai = True
        await session.flush()
        attempt.scoring_status = "pending" if has_ai else "complete"
        await ScoringService.recompute(session, attempt.org_id, attempt.id)
        if has_ai:
            for hook in hooks.ai_scoring_hooks:
                await hook(ctx, session, attempt.id)

    @staticmethod
    async def recompute(session: AsyncSession, org_id: UUID, attempt_id: UUID) -> None:
        """Totals, percent and pass from the answer rows; ``complete`` once no AI answer is unscored."""
        attempt = await repo.get_attempt(session, org_id, attempt_id)
        version: PublishedVersion = await get_published_version(
            session, org_id, attempt.assessment_version_id
        )
        modes = {q.id: q.scoring_mode for q in version.questions}
        rows = await repo.answers(session, attempt.id)
        zero = Decimal("0.00")
        attempt.auto_score = money(sum((r.auto_marks or zero for r in rows), zero))
        attempt.ai_score = money(sum((r.ai_marks or zero for r in rows), zero))
        attempt.final_score = money(sum((r.final_marks or zero for r in rows), zero))
        total = Decimal(version.total_marks)
        attempt.score_percent = money(attempt.final_score * 100 / total) if total > 0 else zero
        attempt.passed = attempt.score_percent >= Decimal(version.pass_mark_percent)
        pending_ai = any(modes.get(r.question_id) == "ai" and r.final_marks is None for r in rows)
        if not pending_ai:
            attempt.scoring_status = "complete"
        elif attempt.scoring_status == "complete":
            attempt.scoring_status = "pending"
        await session.flush()


async def _score_on_submit(ctx: TenantContext, session: AsyncSession, attempt_id: UUID) -> None:
    await ScoringService.score_attempt(ctx, session, attempt_id)


if _score_on_submit not in exam_hooks.on_submit:
    exam_hooks.on_submit.append(_score_on_submit)
