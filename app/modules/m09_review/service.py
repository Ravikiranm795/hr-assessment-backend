"""M9 rules: reports (audited), overrides, decisions, extra time, privacy erase."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, security
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams
from app.core.storage import get_storage
from app.db import Attempt, Decision
from app.modules.m05_assessments.service import get_published_version
from app.modules.m09_review import hooks, repo
from app.modules.m09_review.schemas import (
    AnswerRow,
    AttemptReport,
    AttemptRow,
    AttemptRowLimited,
    DecisionIn,
    DecisionRow,
    DismissIn,
    ErasedOut,
    EventRow,
    ExtraTimeIn,
    OverrideIn,
    ScoreOverrideIn,
    ScoringOut,
    SnapshotUrlOut,
)
from app.modules.m09_review.scoring import ScoringService, money

log = logging.getLogger("app.review")


def _now() -> datetime:
    return datetime.now(UTC)


def _decrypt(blob: str | None) -> str | None:
    try:
        return security.decrypt_field(blob) if blob else None
    except ValueError:
        return None


async def _attempt(session: AsyncSession, org_id: UUID, attempt_id: UUID, *, lock: bool = False) -> Attempt:
    attempt = await repo.get_attempt(session, org_id, attempt_id, lock=lock)
    if attempt is None:
        raise AppError("NOT_FOUND")
    return attempt


def _scoring_out(attempt: Attempt) -> ScoringOut:
    return ScoringOut(
        attempt_id=attempt.id,
        scoring_status=attempt.scoring_status,
        score_percent=attempt.score_percent,
        passed=attempt.passed,
    )


class ReviewService:
    # ------------------------------------------------------------------ lists and report

    @staticmethod
    async def list_for_role(
        ctx: TenantContext, session: AsyncSession, role_id: UUID, params: PageParams
    ) -> tuple[list[AttemptRow] | list[AttemptRowLimited], int]:
        owner = await repo.role_owner(session, ctx.org_id, role_id)
        if owner is None:
            raise AppError("NOT_FOUND")
        rows, total = await repo.list_role_attempts(
            session, ctx.org_id, role_id, offset=params.offset, limit=params.limit
        )
        if ctx.role != "org_admin" and owner != ctx.user_id:
            return [
                AttemptRowLimited(
                    id=r["id"], candidate_name=r["candidate_name"], status=r["status"], decision=r["decision"]
                )
                for r in rows
            ], total
        return [
            AttemptRow(
                **{k: v for k, v in r.items() if k != "candidate_email_enc"},
                candidate_email=_decrypt(r["candidate_email_enc"]),
            )
            for r in rows
        ], total

    @staticmethod
    async def get_report(ctx: TenantContext, session: AsyncSession, attempt_id: UUID) -> AttemptReport:
        """The full report. Viewing it is audited (``ATTEMPT_VIEWED``)."""
        attempt = await _attempt(session, ctx.org_id, attempt_id)
        invite = await repo.invite_for(session, ctx.org_id, attempt.invite_id)
        version = await get_published_version(session, ctx.org_id, attempt.assessment_version_id)
        rows = {a.question_id: a for a in await repo.answers(session, attempt.id)}
        breakdown = []
        for q in version.questions:
            a = rows.get(q.id)
            breakdown.append(
                AnswerRow(
                    question_id=q.id,
                    position=q.position,
                    type=q.type,
                    prompt_html=q.prompt_html,
                    marks=q.marks,
                    scoring_mode=q.scoring_mode,
                    options=[
                        {"id": str(o.id), "text_html": o.text_html, "is_correct": o.is_correct}
                        for o in q.options
                    ],
                    answer_key=q.answer_key,
                    model_answer=q.model_answer,
                    answer=a.answer if a else None,
                    auto_marks=a.auto_marks if a else None,
                    ai_marks=a.ai_marks if a else None,
                    ai_reason=a.ai_reason if a else None,
                    manual_marks=a.manual_marks if a else None,
                    manual_reason=a.manual_reason if a else None,
                    final_marks=a.final_marks if a else None,
                    scored_by=a.scored_by if a else None,
                )
            )
        events = await repo.events(session, ctx.org_id, attempt.id)
        history = await repo.decisions(session, ctx.org_id, attempt.id)
        await audit.record(ctx, session, audit.ATTEMPT_VIEWED, "attempt", attempt.id)
        return AttemptReport(
            id=attempt.id,
            status=attempt.status,
            scoring_status=attempt.scoring_status,
            started_at=attempt.started_at,
            deadline_at=attempt.deadline_at,
            submitted_at=attempt.submitted_at,
            submit_reason=attempt.submit_reason,
            auto_score=attempt.auto_score,
            ai_score=attempt.ai_score,
            final_score=attempt.final_score,
            score_percent=attempt.score_percent,
            passed=attempt.passed,
            total_marks=version.total_marks,
            pass_mark_percent=version.pass_mark_percent,
            decision=attempt.decision,
            decision_note=attempt.decision_note,
            candidate={
                "name": invite["candidate_name"],
                "email": _decrypt(invite["candidate_email_enc"]),
                "phone": _decrypt(invite["candidate_phone_enc"]),
                "erased": invite["pii_purged_at"] is not None,
            },
            badges={
                "camera_exempt": invite["camera_exempt"],
                "extra_time": invite["extra_minutes"] > 0 or invite["time_multiplier"] != 1,
                "auto_submitted_by_timer": attempt.submit_reason == "timer",
                "accommodation_reason": invite["accommodation_reason"],
            },
            consent={"at": attempt.consent_at, "document_version_id": attempt.consent_document_version_id},
            breakdown=breakdown,
            flag_counts=attempt.flag_counts or {},
            timeline=[EventRow.model_validate(e, from_attributes=True) for e in events],
            snapshot_asset_ids=[e.snapshot_asset_id for e in events if e.snapshot_asset_id],
            baseline_asset_id=attempt.baseline_asset_id,
            help_requests=await repo.help_requests(session, ctx.org_id, invite["id"]),
            decision_history=[DecisionRow.model_validate(d, from_attributes=True) for d in history],
        )

    @staticmethod
    async def snapshot_url(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, asset_id: UUID
    ) -> SnapshotUrlOut:
        attempt = await _attempt(session, ctx.org_id, attempt_id)
        allowed = {e.snapshot_asset_id for e in await repo.events(session, ctx.org_id, attempt.id)}
        allowed.add(attempt.baseline_asset_id)
        if asset_id not in allowed:
            raise AppError("NOT_FOUND")
        key = (await repo.asset_keys(session, ctx.org_id, [asset_id])).get(asset_id)
        if key is None:
            raise AppError("NOT_FOUND")
        url = await get_storage().signed_url(key, ttl_sec=60)
        await audit.record(
            ctx, session, audit.SNAPSHOT_VIEWED, "attempt", attempt.id, after={"asset_id": asset_id}
        )
        return SnapshotUrlOut(url=url)

    # ------------------------------------------------------------------ scoring overrides

    @staticmethod
    async def override_score(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, question_id: UUID, body: ScoreOverrideIn
    ) -> ScoringOut:
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt.status != "submitted":
            raise AppError("CONFLICT", details={"reason": "attempt_not_submitted"})
        version = await get_published_version(session, ctx.org_id, attempt.assessment_version_id)
        question = next((q for q in version.questions if q.id == question_id), None)
        if question is None:
            raise AppError("NOT_FOUND")
        if body.marks > Decimal(question.marks):
            raise validation_error([{"field": "marks", "code": "above_question_marks"}])
        row = next((a for a in await repo.answers(session, attempt.id) if a.question_id == question_id), None)
        if row is None:
            raise AppError("NOT_FOUND")
        before = {"final_marks": row.final_marks, "scored_by": row.scored_by}
        row.manual_marks, row.manual_reason = money(body.marks), body.reason
        row.final_marks, row.scored_by = money(body.marks), "manual"
        await session.flush()
        await ScoringService.recompute(session, ctx.org_id, attempt.id)
        await audit.record(
            ctx,
            session,
            audit.SCORE_OVERRIDDEN,
            "attempt_answer",
            row.id,
            before=before,
            after={"final_marks": row.final_marks, "scored_by": "manual", "question_id": question_id},
            reason=body.reason,
        )
        return _scoring_out(attempt)

    @staticmethod
    async def rescore(ctx: TenantContext, session: AsyncSession, attempt_id: UUID) -> ScoringOut:
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt.status != "submitted":
            raise AppError("CONFLICT", details={"reason": "attempt_not_submitted"})
        if not hooks.ai_scoring_hooks:
            raise AppError("AI_NOT_CONFIGURED")
        attempt.scoring_status = "pending"
        await session.flush()
        for hook in hooks.ai_scoring_hooks:
            await hook(ctx, session, attempt.id)
        await session.refresh(attempt)
        return _scoring_out(attempt)

    # ------------------------------------------------------------------ flags and time

    @staticmethod
    async def dismiss_flag(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, event_id: UUID, body: DismissIn
    ) -> EventRow:
        attempt = await _attempt(session, ctx.org_id, attempt_id)
        event = await repo.get_event(session, ctx.org_id, attempt.id, event_id)
        if event is None:
            raise AppError("NOT_FOUND")
        event.dismissed, event.dismissed_by, event.dismissed_at = True, ctx.user_id, _now()
        event.dismissed_note = body.note
        await session.flush()
        await audit.record(
            ctx,
            session,
            audit.FLAG_DISMISSED,
            "proctor_event",
            event.id,
            after={"attempt_id": attempt.id, "type": event.type, "severity": event.severity},
            reason=body.note,
        )
        return EventRow.model_validate(event, from_attributes=True)

    @staticmethod
    async def extra_time(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, body: ExtraTimeIn
    ) -> dict:
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt.status != "in_progress" or attempt.deadline_at is None:
            raise AppError("CONFLICT", details={"reason": "attempt_not_in_progress"})
        before = attempt.deadline_at
        attempt.deadline_at = before + timedelta(minutes=body.minutes)
        await session.execute(
            text("UPDATE invites SET extra_minutes = extra_minutes + :m WHERE id = :i AND org_id = :o"),
            {"m": body.minutes, "i": attempt.invite_id, "o": ctx.org_id},
        )
        await session.flush()
        await audit.record(
            ctx,
            session,
            audit.EXTRA_TIME_GRANTED,
            "attempt",
            attempt.id,
            before={"deadline_at": before},
            after={"deadline_at": attempt.deadline_at, "minutes": body.minutes},
            reason=body.reason,
        )
        return {"attempt_id": attempt.id, "deadline_at": attempt.deadline_at}

    # ------------------------------------------------------------------ decisions

    @staticmethod
    async def decide(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, body: DecisionIn
    ) -> DecisionRow:
        """Owner only, once. The organization admin uses the override."""
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        if await repo.attempt_owner(session, ctx.org_id, attempt.id) != ctx.user_id:
            raise AppError("FORBIDDEN")
        if attempt.status != "submitted":
            raise AppError("CONFLICT", details={"reason": "attempt_not_submitted"})
        if attempt.decision != "waiting":
            raise AppError("DECISION_ALREADY_MADE")
        row = await ReviewService._record(ctx, session, attempt, body.decision, body.note, override=False)
        await audit.record(
            ctx,
            session,
            audit.DECISION_MADE,
            "attempt",
            attempt.id,
            before={"decision": "waiting"},
            after={"decision": body.decision},
            reason=body.note,
        )
        return row

    @staticmethod
    async def override(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, body: OverrideIn
    ) -> DecisionRow:
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt.decision == "waiting":
            raise AppError("CONFLICT", details={"reason": "no_decision_to_override"})
        if body.decision == attempt.decision:
            raise validation_error([{"field": "decision", "code": "unchanged"}])
        before = attempt.decision
        row = await ReviewService._record(ctx, session, attempt, body.decision, body.reason, override=True)
        await audit.record(
            ctx,
            session,
            audit.DECISION_OVERRIDDEN,
            "attempt",
            attempt.id,
            before={"decision": before},
            after={"decision": body.decision},
            reason=body.reason,
        )
        return row

    @staticmethod
    async def _record(
        ctx: TenantContext,
        session: AsyncSession,
        attempt: Attempt,
        decision: str,
        reason: str | None,
        *,
        override: bool,
    ) -> DecisionRow:
        row = Decision(
            org_id=ctx.org_id,
            attempt_id=attempt.id,
            decision=decision,
            decided_by=ctx.user_id,
            reason=reason,
            is_override=override,
            previous_decision=attempt.decision,
        )
        session.add(row)
        attempt.decision, attempt.decided_by, attempt.decided_at = decision, ctx.user_id, _now()
        attempt.decision_note = reason
        await session.flush()
        return DecisionRow.model_validate(row, from_attributes=True)

    @staticmethod
    async def history(org_id: UUID, session: AsyncSession, attempt_id: UUID) -> list[DecisionRow]:
        await _attempt(session, org_id, attempt_id)
        return [
            DecisionRow.model_validate(d, from_attributes=True)
            for d in await repo.decisions(session, org_id, attempt_id)
        ]

    # ------------------------------------------------------------------ privacy erase

    @staticmethod
    async def erase_candidate_data(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, *, include_identity: bool = False
    ) -> ErasedOut:
        """Remove answers, snapshots, events and the phone (and name and email on request). Scores,
        decision and flag counts stay as the summary. Also used by the retention purge."""
        attempt = await _attempt(session, ctx.org_id, attempt_id, lock=True)
        events = await repo.events(session, ctx.org_id, attempt.id)
        asset_ids = [e.snapshot_asset_id for e in events if e.snapshot_asset_id]
        if attempt.baseline_asset_id:
            asset_ids.append(attempt.baseline_asset_id)
        keys = list((await repo.asset_keys(session, ctx.org_id, asset_ids)).values())
        answers = await repo.delete_answers(session, attempt.id)
        removed_events = await repo.delete_events(session, ctx.org_id, attempt.id)
        attempt.baseline_asset_id, attempt.purged_at = None, _now()
        await session.flush()
        await repo.delete_assets(session, ctx.org_id, asset_ids)
        identity = (
            ", candidate_name = 'Erased', candidate_email_enc = NULL, candidate_email_hash = :scrub"
            if include_identity
            else ""
        )
        await session.execute(
            text(
                "UPDATE invites SET candidate_phone_enc = NULL, pii_purged_at = now()"
                + identity
                + " WHERE id = :i AND org_id = :o"
            ),
            {"i": attempt.invite_id, "o": ctx.org_id, "scrub": security.hmac_hash(uuid.uuid4().hex)},
        )
        for key in keys:  # after the rows: a failed file delete must not resurrect database rows
            try:
                await get_storage().delete(key)
            except Exception:  # noqa: BLE001
                log.warning("snapshot file delete failed key=%s", key)
        await audit.record(
            ctx,
            session,
            audit.CANDIDATE_DATA_DELETED,
            "attempt",
            attempt.id,
            after={
                "answers": answers,
                "events": removed_events,
                "snapshots": len(keys),
                "identity_erased": include_identity,
            },
        )
        return ErasedOut(
            answers=answers, events=removed_events, snapshots=len(keys), identity_erased=include_identity
        )
