"""M5b routes (stretch). Org-wide bank: OA and HR read; owner or OA edits."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.core.db import TenantContext
from app.core.deps import DbSession, current_ctx, require_feature, require_owner_or_admin, require_role
from app.core.errors import AppError
from app.core.flags import QUESTION_BANK
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN
from app.modules.m05_assessments.schemas import QuestionType
from app.modules.m05b_question_bank import repo
from app.modules.m05b_question_bank.schemas import AsQuestionOut, BankItemCreate, BankItemOut, BankItemPatch
from app.modules.m05b_question_bank.service import QuestionBankService

router = APIRouter(
    prefix="/api/v1/question-bank",
    tags=["question-bank"],
    dependencies=[Depends(require_feature(QUESTION_BANK))],
)

Member = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]
_ERR = {
    401: {"description": "UNAUTHENTICATED"},
    403: {"description": "FORBIDDEN, FEATURE_DISABLED"},
    404: {"description": "NOT_FOUND (also for another organization's id)"},
}


async def _item_owner(
    item_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    if ctx.role not in (ORG_ADMIN, HR_USER):
        raise AppError("FORBIDDEN")
    return await repo.owner_id(session, ctx.org_id, item_id)


Editor = Annotated[TenantContext, Depends(require_owner_or_admin(_item_owner))]


@router.get(
    "",
    response_model=Page[BankItemOut],
    summary="List question bank items",
    description="Items saved by anyone in the organization. Filter by `q` (prompt text), `tag` and `type`.",
    responses=_ERR,
)
async def list_items(
    ctx: Member,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
    type: QuestionType | None = None,  # noqa: A002
):
    items, total = await QuestionBankService.search(
        ctx.org_id, session, params, q=q, tag=tag.lower() if tag else None, qtype=type
    )
    return Page[BankItemOut].build(items, total=total, params=params)


@router.post(
    "",
    response_model=BankItemOut,
    status_code=201,
    summary="Save a question to the bank",
    description="Same body as an assessment question plus `tags`. The caller becomes the owner.",
    responses={**_ERR, 422: {"description": "VALIDATION_ERROR, QUESTION_INVALID, MODEL_ANSWER_REQUIRED"}},
)
async def create_item(body: BankItemCreate, ctx: Member, session: DbSession):
    return await QuestionBankService.create(ctx, session, body)


@router.get(
    "/{item_id}",
    response_model=BankItemOut,
    summary="Question bank item",
    description="One item, including its answer key (the bank is only visible to OA and HR).",
    responses=_ERR,
)
async def get_item(item_id: UUID, ctx: Member, session: DbSession):
    return await QuestionBankService.get(ctx.org_id, session, item_id)


@router.get(
    "/{item_id}/as-question",
    response_model=AsQuestionOut,
    summary="Convert an item to a question body",
    description="Returns the body for `POST /assessments/{id}/versions/{vid}/questions`; "
    "the frontend posts it.",
    responses=_ERR,
)
async def item_as_question(item_id: UUID, ctx: Member, session: DbSession):
    return await QuestionBankService.as_question(ctx.org_id, session, item_id)


@router.patch(
    "/{item_id}",
    response_model=BankItemOut,
    summary="Edit a bank item",
    description="Owner or org admin.",
    responses={**_ERR, 422: {"description": "VALIDATION_ERROR, QUESTION_INVALID"}},
)
async def patch_item(item_id: UUID, body: BankItemPatch, ctx: Editor, session: DbSession):
    return await QuestionBankService.update(ctx, session, item_id, body)


@router.delete(
    "/{item_id}",
    status_code=204,
    summary="Delete a bank item",
    description="Owner or org admin. Questions already copied into assessments are unaffected.",
    responses=_ERR,
)
async def delete_item(item_id: UUID, ctx: Editor, session: DbSession):
    await QuestionBankService.delete(ctx, session, item_id)
    return Response(status_code=204)
