"""M5 routes (spec 6/M5). Thin: validate, call the service, shape the response."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.core.db import TenantContext
from app.core.deps import DbSession, current_ctx, require_feature, require_owner_or_admin, require_role
from app.core.errors import AppError, validation_error
from app.core.flags import ASSESSMENTS
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN
from app.modules.m05_assessments import repo
from app.modules.m05_assessments.schemas import (
    AssessmentCreate,
    AssessmentDetail,
    AssessmentListItem,
    AssessmentPatch,
    AssessmentStatus,
    BulkQuestionsIn,
    PreviewOut,
    QuestionIn,
    QuestionList,
    QuestionOut,
    QuestionPatch,
    ReorderIn,
    VersionList,
    VersionOut,
    VersionPatch,
)
from app.modules.m05_assessments.service import AssessmentService

router = APIRouter(
    prefix="/api/v1/assessments",
    tags=["assessments"],
    dependencies=[Depends(require_feature(ASSESSMENTS))],
)


def _errors(*codes: str) -> dict[int | str, dict]:
    """Error responses for Swagger, grouped by status, each with an envelope example."""
    from app.core.errors import ERROR_CODES

    grouped: dict[int, list[str]] = {}
    for code in codes:
        grouped.setdefault(ERROR_CODES[code].status, []).append(code)
    out: dict[int | str, dict] = {}
    for status, names in grouped.items():
        first = ERROR_CODES[names[0]]
        out[status] = {
            "description": ", ".join(names),
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": first.code,
                            "message_key": first.message_key,
                            "details": {},
                            "request_id": "3f2c9a1e",
                        }
                    }
                }
            },
        }
    return out


COMMON = ("UNAUTHENTICATED", "FEATURE_DISABLED", "NOT_FOUND")
EDIT = (*COMMON, "FORBIDDEN")

Writer = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]


@dataclass(frozen=True)
class Scope:
    ctx: TenantContext
    org_id: UUID


async def read_scope(
    ctx: Annotated[TenantContext, Depends(require_role(SUPER_ADMIN, ORG_ADMIN, HR_USER))],
    org_id: Annotated[
        UUID | None, Query(description="Super admin only (read-only access): the organization to read.")
    ] = None,
) -> Scope:
    if ctx.org_id is not None:
        return Scope(ctx, ctx.org_id)
    if org_id is None:
        raise validation_error([{"field": "org_id", "code": "required"}])
    return Scope(ctx, org_id)


Reader = Annotated[Scope, Depends(read_scope)]


async def assessment_owner(
    assessment_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    if ctx.role not in (ORG_ADMIN, HR_USER):
        raise AppError("FORBIDDEN")
    return await repo.owner_id(session, ctx.org_id, assessment_id)


Editor = Annotated[TenantContext, Depends(require_owner_or_admin(assessment_owner))]


# --------------------------------------------------------------------------- assessments


@router.get(
    "",
    response_model=Page[AssessmentListItem],
    summary="List assessments",
    description="Search by title, filter by status or owner, `mine`, and `linkable` (active with a published "
    "version). Super admins read one organization via `org_id`.",
    responses=_errors(*COMMON),
)
async def list_assessments(
    scope: Reader,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: AssessmentStatus | None = None,
    owner_id: UUID | None = None,
    mine: bool = False,
    linkable: bool = False,
):
    owner = scope.ctx.user_id if mine else owner_id
    items, total = await AssessmentService.search(
        scope.org_id, session, params, q=q, status=status, owner_id=owner, linkable=linkable
    )
    return Page[AssessmentListItem].build(items, total=total, params=params)


@router.post(
    "",
    response_model=AssessmentDetail,
    status_code=201,
    summary="Create an assessment",
    description="Creates the assessment and its version 1 draft. The caller becomes the owner.",
    responses=_errors(*EDIT, "VALIDATION_ERROR"),
)
async def create_assessment(body: AssessmentCreate, ctx: Writer, session: DbSession):
    return await AssessmentService.create(ctx, session, body)


@router.get(
    "/{assessment_id}",
    response_model=AssessmentDetail,
    summary="Assessment detail",
    description="Assessment with a summary of each version (`in_use` marks versions protected from purge).",
    responses=_errors(*COMMON),
)
async def get_assessment(assessment_id: UUID, scope: Reader, session: DbSession):
    return await AssessmentService.get(scope.org_id, session, assessment_id)


@router.patch(
    "/{assessment_id}",
    response_model=AssessmentDetail,
    summary="Edit title and description",
    description="Owner or org admin.",
    responses=_errors(*EDIT, "VALIDATION_ERROR"),
)
async def patch_assessment(assessment_id: UUID, body: AssessmentPatch, ctx: Editor, session: DbSession):
    return await AssessmentService.update(ctx, session, assessment_id, body)


@router.delete(
    "/{assessment_id}",
    status_code=204,
    summary="Soft delete an assessment",
    description="Owner or org admin. Blocked with `CONFLICT` "
    "(`details.role_ids`) while a version is linked to "
    "a role that is not closed.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def delete_assessment(assessment_id: UUID, ctx: Editor, session: DbSession):
    await AssessmentService.delete(ctx, session, assessment_id)
    return Response(status_code=204)


@router.post(
    "/{assessment_id}/duplicate",
    response_model=AssessmentDetail,
    status_code=201,
    summary="Duplicate an assessment",
    description="Copies the latest version into a new assessment owned by the caller (version 1 draft).",
    responses=_errors(*EDIT),
)
async def duplicate_assessment(assessment_id: UUID, ctx: Writer, session: DbSession):
    return await AssessmentService.duplicate(ctx, session, assessment_id)


@router.post(
    "/{assessment_id}/archive",
    response_model=AssessmentDetail,
    summary="Archive an assessment",
    description="Owner or org admin.",
    responses=_errors(*EDIT),
)
async def archive_assessment(assessment_id: UUID, ctx: Editor, session: DbSession):
    return await AssessmentService.set_archived(ctx, session, assessment_id, True)


@router.post(
    "/{assessment_id}/unarchive",
    response_model=AssessmentDetail,
    summary="Unarchive an assessment",
    description="Owner or org admin.",
    responses=_errors(*EDIT),
)
async def unarchive_assessment(assessment_id: UUID, ctx: Editor, session: DbSession):
    return await AssessmentService.set_archived(ctx, session, assessment_id, False)


# --------------------------------------------------------------------------- versions


@router.get(
    "/{assessment_id}/versions",
    response_model=VersionList,
    summary="List versions",
    description="Version summaries, newest first.",
    responses=_errors(*COMMON),
)
async def list_versions(assessment_id: UUID, scope: Reader, session: DbSession):
    return await AssessmentService.list_versions(scope.org_id, session, assessment_id)


@router.post(
    "/{assessment_id}/versions",
    response_model=VersionOut,
    status_code=201,
    summary="Create a new draft version",
    description="Cloned from the latest published version. With three versions the oldest unprotected one is "
    "purged first. Errors: `DRAFT_ALREADY_EXISTS`, `VERSION_LIMIT_REACHED_ALL_IN_USE`.",
    responses=_errors(*EDIT, "DRAFT_ALREADY_EXISTS", "VERSION_LIMIT_REACHED_ALL_IN_USE"),
)
async def create_version(assessment_id: UUID, ctx: Editor, session: DbSession):
    return await AssessmentService.create_version(ctx, session, assessment_id)


@router.get(
    "/{assessment_id}/versions/{version_id}",
    response_model=VersionOut,
    summary="Version with questions",
    description="Full content including answer keys (staff only).",
    responses=_errors(*COMMON),
)
async def get_version(assessment_id: UUID, version_id: UUID, scope: Reader, session: DbSession):
    return await AssessmentService.get_version(scope.org_id, session, assessment_id, version_id)


@router.patch(
    "/{assessment_id}/versions/{version_id}",
    response_model=VersionOut,
    summary="Edit a draft version",
    description="Duration (5-480), pass mark (0-100), "
    "instructions, header, footer, proctoring config, shuffle "
    "settings. Published versions return `ASSESSMENT_IMMUTABLE`.",
    responses=_errors(*EDIT, "ASSESSMENT_IMMUTABLE", "VALIDATION_ERROR"),
)
async def patch_version(
    assessment_id: UUID, version_id: UUID, body: VersionPatch, ctx: Editor, session: DbSession
):
    return await AssessmentService.update_version(ctx, session, assessment_id, version_id, body)


@router.post(
    "/{assessment_id}/versions/{version_id}/publish",
    response_model=VersionOut,
    summary="Publish a version",
    description="Validates (questions, marks, options, answers, "
    "model answers, duration, pass mark) then freezes "
    "the version. `QUESTION_INVALID` and `MODEL_ANSWER_REQUIRED` carry `details.issues`.",
    responses=_errors(
        *EDIT, "ASSESSMENT_IMMUTABLE", "QUESTION_INVALID", "MODEL_ANSWER_REQUIRED", "VALIDATION_ERROR"
    ),
)
async def publish_version(assessment_id: UUID, version_id: UUID, ctx: Editor, session: DbSession):
    return await AssessmentService.publish_version(ctx, session, assessment_id, version_id)


@router.get(
    "/{assessment_id}/versions/{version_id}/preview",
    response_model=PreviewOut,
    summary="Candidate preview",
    description="Exactly what a candidate will see: branding "
    "header, instructions, footer, watermark (only while "
    "the watermark flag is on) and questions without answer keys.",
    responses=_errors(*COMMON),
)
async def preview_version(assessment_id: UUID, version_id: UUID, scope: Reader, session: DbSession):
    return await AssessmentService.preview(scope.org_id, session, assessment_id, version_id)


# --------------------------------------------------------------------------- questions

QBASE = "/{assessment_id}/versions/{version_id}/questions"
Q_ERRORS = _errors(*EDIT, "ASSESSMENT_IMMUTABLE", "QUESTION_INVALID", "MODEL_ANSWER_REQUIRED")


@router.post(
    f"{QBASE}/bulk",
    response_model=QuestionList,
    status_code=201,
    summary="Add several questions",
    description="Adds the ticked AI suggestions. All items are validated first; one invalid item rejects the "
    "batch (`details.index`).",
    responses=Q_ERRORS,
)
async def bulk_questions(
    assessment_id: UUID, version_id: UUID, body: BulkQuestionsIn, ctx: Editor, session: DbSession
):
    return await AssessmentService.bulk_add(ctx, session, assessment_id, version_id, body)


@router.put(
    f"{QBASE}/reorder",
    response_model=QuestionList,
    summary="Reorder questions",
    description="Body lists every question id of the draft exactly once, in the new order.",
    responses=_errors(*EDIT, "ASSESSMENT_IMMUTABLE", "VALIDATION_ERROR"),
)
async def reorder_questions(
    assessment_id: UUID, version_id: UUID, body: ReorderIn, ctx: Editor, session: DbSession
):
    return await AssessmentService.reorder(ctx, session, assessment_id, version_id, body.question_ids)


@router.post(
    QBASE,
    response_model=QuestionOut,
    status_code=201,
    summary="Add a question",
    description="Appends to a draft version. Prompts and options "
    "are sanitized. AI-scored questions need a model "
    "answer or key points (`MODEL_ANSWER_REQUIRED`).",
    responses=Q_ERRORS,
)
async def add_question(
    assessment_id: UUID, version_id: UUID, body: QuestionIn, ctx: Editor, session: DbSession
):
    return await AssessmentService.add_question(ctx, session, assessment_id, version_id, body)


@router.patch(
    f"{QBASE}/{{question_id}}",
    response_model=QuestionOut,
    summary="Edit a question",
    description="Draft versions only. Provided fields replace "
    "the stored ones; `options`, when given, replaces "
    "the whole option list.",
    responses=Q_ERRORS,
)
async def patch_question(
    assessment_id: UUID,
    version_id: UUID,
    question_id: UUID,
    body: QuestionPatch,
    ctx: Editor,
    session: DbSession,
):
    return await AssessmentService.update_question(ctx, session, assessment_id, version_id, question_id, body)


@router.delete(
    f"{QBASE}/{{question_id}}",
    status_code=204,
    summary="Delete a question",
    description="Draft versions only. Remaining questions are renumbered.",
    responses=_errors(*EDIT, "ASSESSMENT_IMMUTABLE"),
)
async def delete_question(
    assessment_id: UUID, version_id: UUID, question_id: UUID, ctx: Editor, session: DbSession
):
    await AssessmentService.delete_question(ctx, session, assessment_id, version_id, question_id)
    return Response(status_code=204)
