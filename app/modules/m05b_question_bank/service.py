"""M5b rules. Validation reuses M5's ``normalize_question`` so bank items and questions never diverge."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import TenantContext
from app.core.errors import AppError
from app.core.pagination import PageParams, parse_sort
from app.db import QuestionBankItem as Item
from app.modules.m05_assessments.question_rules import normalize_question
from app.modules.m05_assessments.schemas import OptionIn, QuestionIn
from app.modules.m05b_question_bank import repo
from app.modules.m05b_question_bank.schemas import BankItemCreate, BankItemOut, BankItemPatch

_SORTABLE = {"created_at", "updated_at", "marks"}
_ENTITY = "question_bank_item"


def _out(row: Item) -> BankItemOut:
    return BankItemOut(
        id=row.id,
        owner_id=row.owner_id,
        type=row.type,
        prompt_html=row.prompt_html,
        marks=row.marks,
        scoring_mode=row.scoring_mode,
        options=[OptionIn(**o) for o in row.options or []],
        answer_key=row.answer_key,
        model_answer=row.model_answer,
        code_language=row.code_language,
        tags=list(row.tags),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _question_body(row: Item, **changes) -> QuestionIn:
    data = {
        "type": row.type,
        "prompt_html": row.prompt_html,
        "marks": row.marks,
        "scoring_mode": row.scoring_mode,
        "options": [OptionIn(**o) for o in row.options or []],
        "answer_key": row.answer_key,
        "model_answer": row.model_answer,
        "code_language": row.code_language,
    }
    return QuestionIn.model_validate({**data, **changes})


def _apply(row: Item, body: QuestionIn) -> None:
    n = normalize_question(body)
    row.type, row.prompt_html, row.marks, row.scoring_mode = n.type, n.prompt_html, n.marks, n.scoring_mode
    row.answer_key, row.model_answer, row.code_language = n.answer_key, n.model_answer, n.code_language
    row.options = [{"text_html": text, "is_correct": ok} for text, ok in n.options]


async def _get(session: AsyncSession, org_id: UUID, item_id: UUID) -> Item:
    row = await repo.get(session, org_id, item_id)
    if row is None:
        raise AppError("NOT_FOUND")
    return row


class QuestionBankService:
    @staticmethod
    async def create(ctx: TenantContext, session: AsyncSession, body: BankItemCreate) -> BankItemOut:
        row = Item(org_id=ctx.org_id, owner_id=ctx.user_id, created_by=ctx.user_id, tags=_tags(body.tags))
        _apply(row, QuestionIn.model_validate(body.model_dump(exclude={"tags"})))
        session.add(row)
        await session.flush()
        await audit.record(ctx, session, audit.QUESTION_ADDED, _ENTITY, row.id, after={"type": row.type})
        return _out(row)

    @staticmethod
    async def get(org_id: UUID, session: AsyncSession, item_id: UUID) -> BankItemOut:
        return _out(await _get(session, org_id, item_id))

    @staticmethod
    async def as_question(org_id: UUID, session: AsyncSession, item_id: UUID) -> QuestionIn:
        return _question_body(await _get(session, org_id, item_id))

    @staticmethod
    async def search(
        org_id: UUID,
        session: AsyncSession,
        params: PageParams,
        *,
        q: str | None,
        tag: str | None,
        qtype: str | None,
    ) -> tuple[list[BankItemOut], int]:
        sort = parse_sort(params.sort, _SORTABLE, default="created_at:desc")
        rows, total = await repo.search(
            session, org_id, q=q, tag=tag, qtype=qtype, sort=sort, offset=params.offset, limit=params.limit
        )
        return [_out(r) for r in rows], total

    @staticmethod
    async def update(
        ctx: TenantContext, session: AsyncSession, item_id: UUID, body: BankItemPatch
    ) -> BankItemOut:
        row = await _get(session, ctx.org_id, item_id)
        changes = body.model_dump(exclude_unset=True, exclude={"tags"})
        if changes:
            _apply(row, _question_body(row, **changes))
        if body.tags is not None:
            row.tags = _tags(body.tags)
        await session.flush()
        await audit.record(ctx, session, audit.QUESTION_UPDATED, _ENTITY, row.id, after={"type": row.type})
        return _out(row)

    @staticmethod
    async def delete(ctx: TenantContext, session: AsyncSession, item_id: UUID) -> None:
        row = await _get(session, ctx.org_id, item_id)
        await session.delete(row)
        await session.flush()
        await audit.record(ctx, session, audit.QUESTION_DELETED, _ENTITY, item_id)


def _tags(tags: list[str]) -> list[str]:
    return sorted({t.lower() for t in tags})
