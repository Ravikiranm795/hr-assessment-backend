"""M9 routes (spec 6/M9). Owner or org admin; decisions are owner-only, overrides and erase are admin-only.
The super admin has no access to candidate content."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, current_ctx, require_feature, require_owner_or_admin, require_role
from app.core.errors import ERROR_CODES, AppError
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN
from app.modules.m09_review import repo
from app.modules.m09_review.schemas import (
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
from app.modules.m09_review.service import ReviewService

router = APIRouter(prefix="/api/v1", tags=["review"], dependencies=[Depends(require_feature(flags.REVIEW))])


def _errors(*codes: str) -> dict[int | str, dict]:
    grouped: dict[int, list[str]] = {}
    for code in codes:
        grouped.setdefault(ERROR_CODES[code].status, []).append(code)
    out: dict[int | str, dict] = {}
    for status, names in grouped.items():
        spec = ERROR_CODES[names[0]]
        example = {
            "error": {"code": spec.code, "message_key": spec.message_key, "details": {}, "request_id": "r1"}
        }
        out[status] = {"description": ", ".join(names), "content": {"application/json": {"example": example}}}
    return out


BASE = ("UNAUTHENTICATED", "FEATURE_DISABLED", "FORBIDDEN", "NOT_FOUND")
Staff = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]
Admin = Annotated[TenantContext, Depends(require_role(ORG_ADMIN))]


async def attempt_owner(
    attempt_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    if ctx.role not in (ORG_ADMIN, HR_USER):
        raise AppError("FORBIDDEN")
    return await repo.attempt_owner(session, ctx.org_id, attempt_id)


Editor = Annotated[TenantContext, Depends(require_owner_or_admin(attempt_owner))]


@router.get(
    "/roles/{role_id}/attempts",
    response_model=Page[AttemptRow] | Page[AttemptRowLimited],
    summary="List a role's attempts",
    description="Owner and org admin see scores and flag counts; another HR user sees name, status and "
    "decision only. Super admins have no access.",
    responses=_errors(*BASE),
)
async def list_attempts(
    role_id: UUID, ctx: Staff, session: DbSession, params: Annotated[PageParams, Depends(page_params)]
):
    owner = await repo.role_owner(session, ctx.org_id, role_id)
    items, total = await ReviewService.list_for_role(ctx, session, role_id, params)
    page = AttemptRow if ctx.role == ORG_ADMIN or owner == ctx.user_id else AttemptRowLimited
    return Page[page].build(items, total=total, params=params)  # type: ignore[valid-type]


@router.get(
    "/attempts/{attempt_id}",
    response_model=AttemptReport,
    summary="Attempt report",
    description="Score breakdown with answers and keys, violation summary and timeline, snapshot ids, "
    "help-request history, badges and consent record. Every view is audited (`ATTEMPT_VIEWED`).",
    responses=_errors(*BASE),
)
async def get_report(attempt_id: UUID, ctx: Editor, session: DbSession):
    return await ReviewService.get_report(ctx, session, attempt_id)


@router.get(
    "/attempts/{attempt_id}/snapshots/{asset_id}/url",
    response_model=SnapshotUrlOut,
    summary="Signed snapshot URL",
    description="A 60-second signed URL for a snapshot or the baseline photo of this attempt. Audited.",
    responses=_errors(*BASE),
)
async def snapshot_url(attempt_id: UUID, asset_id: UUID, ctx: Editor, session: DbSession):
    return await ReviewService.snapshot_url(ctx, session, attempt_id, asset_id)


@router.patch(
    "/attempts/{attempt_id}/answers/{question_id}/score",
    response_model=ScoringOut,
    summary="Override an answer's score",
    description="Manual or AI-score override with a reason (marks up to the question's marks). Audited.",
    responses=_errors(*BASE, "VALIDATION_ERROR", "CONFLICT"),
)
async def override_score(
    attempt_id: UUID, question_id: UUID, body: ScoreOverrideIn, ctx: Editor, session: DbSession
):
    return await ReviewService.override_score(ctx, session, attempt_id, question_id, body)


@router.post(
    "/attempts/{attempt_id}/rescore",
    response_model=ScoringOut,
    summary="Retry AI scoring",
    description="Runs the AI scoring hooks again for a submitted attempt.",
    responses=_errors(*BASE, "CONFLICT", "AI_NOT_CONFIGURED"),
)
async def rescore(attempt_id: UUID, ctx: Editor, session: DbSession):
    return await ReviewService.rescore(ctx, session, attempt_id)


@router.post(
    "/attempts/{attempt_id}/events/{event_id}/dismiss",
    response_model=EventRow,
    summary="Dismiss a flag",
    description="Marks a proctoring event as dismissed with a note. The event stays in the timeline.",
    responses=_errors(*BASE, "VALIDATION_ERROR"),
)
async def dismiss(attempt_id: UUID, event_id: UUID, body: DismissIn, ctx: Editor, session: DbSession):
    return await ReviewService.dismiss_flag(ctx, session, attempt_id, event_id, body)


@router.post(
    "/attempts/{attempt_id}/extra-time",
    summary="Add extra time",
    description="Moves the deadline of an attempt that is in progress. Reason required; audited.",
    responses={
        200: {
            "description": "The attempt with its new deadline.",
            "content": {
                "application/json": {
                    "example": {
                        "attempt_id": "0b6f5c0e-5d3a-4c8e-9a64-2f1c3e8d7a10",
                        "deadline_at": "2026-11-16T10:45:00Z",
                    }
                }
            },
        },
        **_errors(*BASE, "CONFLICT", "VALIDATION_ERROR"),
    },
)
async def extra_time(attempt_id: UUID, body: ExtraTimeIn, ctx: Editor, session: DbSession):
    return await ReviewService.extra_time(ctx, session, attempt_id, body)


@router.post(
    "/attempts/{attempt_id}/decision",
    response_model=DecisionRow,
    status_code=201,
    summary="Make the decision",
    description="Role owner only, once, for a submitted attempt (`DECISION_ALREADY_MADE` otherwise). "
    "The organization admin uses the override.",
    responses=_errors(*BASE, "DECISION_ALREADY_MADE", "CONFLICT"),
)
async def decide(attempt_id: UUID, body: DecisionIn, ctx: Staff, session: DbSession):
    return await ReviewService.decide(ctx, session, attempt_id, body)


@router.post(
    "/attempts/{attempt_id}/decision/override",
    response_model=DecisionRow,
    status_code=201,
    summary="Override the decision",
    description="Organization admin only, with a reason. History is kept.",
    responses=_errors(*BASE, "CONFLICT", "VALIDATION_ERROR"),
)
async def override(attempt_id: UUID, body: OverrideIn, ctx: Admin, session: DbSession):
    return await ReviewService.override(ctx, session, attempt_id, body)


@router.get(
    "/attempts/{attempt_id}/decision-history",
    response_model=list[DecisionRow],
    summary="Decision history",
    description="Every decision and override, oldest first.",
    responses=_errors(*BASE),
)
async def decision_history(attempt_id: UUID, ctx: Editor, session: DbSession):
    return await ReviewService.history(ctx.org_id, session, attempt_id)


@router.delete(
    "/attempts/{attempt_id}/candidate-data",
    response_model=ErasedOut,
    summary="Erase candidate data",
    description="Organization admin only. Removes answers, snapshots, events and the phone; with "
    "`include_identity` also name and email. Scores and the decision stay. Audited.",
    responses=_errors(*BASE),
)
async def erase(
    attempt_id: UUID, ctx: Admin, session: DbSession, include_identity: Annotated[bool, Query()] = False
):
    return await ReviewService.erase_candidate_data(
        ctx, session, attempt_id, include_identity=include_identity
    )
