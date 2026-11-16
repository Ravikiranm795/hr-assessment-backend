"""M5 business rules. Services take the request's ``TenantContext`` and ``AsyncSession`` (the request
transaction), audit in the same transaction, and never commit.

Interfaces used by other modules:

- ``get_published_version(session, org_id, version_id) -> PublishedVersion`` (M6 linking, M8 exam)
- ``grade_auto(question, answer) -> Decimal`` (M9 scoring; re-exported from ``scoring_defs``)
- ``VersionRules.create_draft`` (``version_rules``)
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, flags
from app.core.branding_text import watermark
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams, parse_sort
from app.db import Assessment, AssessmentVersion, Question, QuestionOption
from app.modules.m05_assessments import repo
from app.modules.m05_assessments.question_rules import (
    ALLOWED_SCORING,
    DEFAULT_SCORING,
    PublishView,
    normalize_question,
    publish_issues,
)
from app.modules.m05_assessments.sanitize import sanitize_html
from app.modules.m05_assessments.schemas import (
    AssessmentCreate,
    AssessmentDetail,
    AssessmentListItem,
    AssessmentPatch,
    BulkQuestionsIn,
    OptionOut,
    PreviewHeader,
    PreviewOption,
    PreviewOut,
    PreviewQuestion,
    QuestionIn,
    QuestionList,
    QuestionOut,
    QuestionPatch,
    VersionList,
    VersionOut,
    VersionPatch,
    VersionSummary,
)
from app.modules.m05_assessments.scoring_defs import GradableOption, GradableQuestion, grade_auto
from app.modules.m05_assessments.version_rules import VersionRules

__all__ = ["AssessmentService", "PublishedVersion", "get_published_version", "grade_auto", "VersionRules"]

SORT_FIELDS = {"title", "created_at", "updated_at"}


# --------------------------------------------------------------------------- published version interface


@dataclass(frozen=True)
class PublishedVersion:
    id: UUID
    assessment_id: UUID
    org_id: UUID
    version_no: int
    duration_minutes: int
    pass_mark_percent: Decimal
    instructions_html: str | None
    header_config: dict
    footer_html: str | None
    proctoring_config: dict
    shuffle_questions: bool
    shuffle_options: bool
    total_marks: Decimal
    published_at: datetime | None
    questions: list[GradableQuestion] = field(default_factory=list)


async def _gradable(session: AsyncSession, org_id: UUID, version_id: UUID) -> list[GradableQuestion]:
    questions = await repo.list_questions(session, org_id, version_id)
    options = await repo.options_by_question(session, [q.id for q in questions])
    return [
        GradableQuestion(
            id=q.id,
            position=q.position,
            type=q.type,
            prompt_html=q.prompt_html,
            marks=q.marks,
            scoring_mode=q.scoring_mode,
            answer_key=q.answer_key,
            model_answer=q.model_answer,
            code_language=q.code_language,
            options=[GradableOption(o.id, o.text_html, o.is_correct, o.position) for o in options[q.id]],
        )
        for q in questions
    ]


async def get_published_version(session: AsyncSession, org_id: UUID, version_id: UUID) -> PublishedVersion:
    """A published version with its questions and answer keys (server side only; strip keys before
    anything reaches a candidate, see ``candidate_view``). A version of another org is ``NOT_FOUND``;
    a draft is ``ASSESSMENT_NOT_PUBLISHED``."""
    version = await repo.get_version_any(session, org_id, version_id)
    if version is None:
        raise AppError("NOT_FOUND")
    if version.status != "published":
        raise AppError("ASSESSMENT_NOT_PUBLISHED")
    return PublishedVersion(
        id=version.id,
        assessment_id=version.assessment_id,
        org_id=version.org_id,
        version_no=version.version_no,
        duration_minutes=version.duration_minutes,
        pass_mark_percent=version.pass_mark_percent,
        instructions_html=version.instructions_html,
        header_config=version.header_config or {},
        footer_html=version.footer_html,
        proctoring_config=version.proctoring_config or {},
        shuffle_questions=version.shuffle_questions,
        shuffle_options=version.shuffle_options,
        total_marks=version.total_marks,
        published_at=version.published_at,
        questions=await _gradable(session, org_id, version.id),
    )


def candidate_questions(questions: list[GradableQuestion]) -> list[PreviewQuestion]:
    """The candidate-safe projection: no correct flags, answer keys, model answers or scoring modes."""
    result = []
    for q in questions:
        key = q.answer_key if isinstance(q.answer_key, dict) else {}
        blanks = len(key.get("blanks", [])) if q.type == "fill_blank" else None
        if q.type == "code_gap":
            blanks = len(key.get("gaps", []))
        result.append(
            PreviewQuestion(
                id=q.id,
                position=q.position,
                type=q.type,
                prompt_html=q.prompt_html,
                marks=q.marks,
                options=[PreviewOption(id=o.id, text_html=o.text_html) for o in q.options],
                blank_count=blanks,
                code_language=q.code_language,
            )
        )
    return result


# --------------------------------------------------------------------------- presenters


def _summary(v: AssessmentVersion, counts: dict[UUID, int], protected: set[UUID]) -> VersionSummary:
    return VersionSummary(
        id=v.id,
        version_no=v.version_no,
        status=v.status,
        duration_minutes=v.duration_minutes,
        pass_mark_percent=v.pass_mark_percent,
        total_marks=v.total_marks,
        question_count=counts.get(v.id, 0),
        in_use=v.id in protected,
        published_at=v.published_at,
        created_at=v.created_at,
    )


def _question_out(q: Question, options: list[QuestionOption]) -> QuestionOut:
    return QuestionOut(
        id=q.id,
        position=q.position,
        type=q.type,
        prompt_html=q.prompt_html,
        marks=q.marks,
        scoring_mode=q.scoring_mode,
        options=[
            OptionOut(id=o.id, position=o.position, text_html=o.text_html, is_correct=o.is_correct)
            for o in options
        ],
        answer_key=q.answer_key,
        model_answer=q.model_answer,
        code_language=q.code_language,
    )


async def _questions_out(session: AsyncSession, org_id: UUID, version_id: UUID) -> list[QuestionOut]:
    questions = await repo.list_questions(session, org_id, version_id)
    options = await repo.options_by_question(session, [q.id for q in questions])
    return [_question_out(q, options[q.id]) for q in questions]


async def _version_out(session: AsyncSession, org_id: UUID, v: AssessmentVersion) -> VersionOut:
    counts = await repo.question_counts(session, [v.id])
    protected = await repo.protected_version_ids(session, org_id, v.assessment_id)
    summary = _summary(v, counts, protected)
    return VersionOut(
        **summary.model_dump(),
        assessment_id=v.assessment_id,
        instructions_html=v.instructions_html,
        header_config=v.header_config or {},
        footer_html=v.footer_html,
        proctoring_config=v.proctoring_config or {},
        shuffle_questions=v.shuffle_questions,
        shuffle_options=v.shuffle_options,
        questions=await _questions_out(session, org_id, v.id),
    )


def _list_item(a: Assessment, versions: list[AssessmentVersion]) -> AssessmentListItem:
    published = [v for v in versions if v.status == "published"]
    latest = max(versions, key=lambda v: v.version_no) if versions else None
    latest_pub = max(published, key=lambda v: v.version_no) if published else None
    return AssessmentListItem(
        id=a.id,
        title=a.title,
        description=a.description,
        owner_id=a.owner_id,
        status=a.status,
        version_count=len(versions),
        latest_version_no=latest.version_no if latest else None,
        latest_published_version_id=latest_pub.id if latest_pub else None,
        has_draft=any(v.status == "draft" for v in versions),
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


async def _detail(session: AsyncSession, org_id: UUID, a: Assessment) -> AssessmentDetail:
    versions = await repo.list_versions(session, org_id, [a.id])
    counts = await repo.question_counts(session, [v.id for v in versions])
    protected = await repo.protected_version_ids(session, org_id, a.id)
    return AssessmentDetail(
        **_list_item(a, versions).model_dump(), versions=[_summary(v, counts, protected) for v in versions]
    )


def _snapshot(a: Assessment) -> dict[str, Any]:
    return {"title": a.title, "description": a.description, "status": a.status, "owner_id": a.owner_id}


# --------------------------------------------------------------------------- lookups with guards


async def _assessment(
    session: AsyncSession, org_id: UUID, assessment_id: UUID, *, lock: bool = False
) -> Assessment:
    row = await repo.get_assessment(session, org_id, assessment_id, lock=lock)
    if row is None:
        raise AppError("NOT_FOUND")
    return row


async def _version(
    session: AsyncSession, org_id: UUID, assessment_id: UUID, version_id: UUID, *, lock: bool = False
) -> AssessmentVersion:
    await _assessment(session, org_id, assessment_id)
    row = await repo.get_version(session, org_id, assessment_id, version_id, lock=lock)
    if row is None:
        raise AppError("NOT_FOUND")
    return row


async def _draft(
    session: AsyncSession, org_id: UUID, assessment_id: UUID, version_id: UUID
) -> AssessmentVersion:
    """The version to be edited: locked for update, and refused once published."""
    row = await _version(session, org_id, assessment_id, version_id, lock=True)
    if row.status != "draft":
        raise AppError("ASSESSMENT_IMMUTABLE")
    return row


# --------------------------------------------------------------------------- service


class AssessmentService:
    # ----- assessments

    @staticmethod
    async def create(ctx: TenantContext, session: AsyncSession, body: AssessmentCreate) -> AssessmentDetail:
        assessment = await repo.insert_assessment(
            session,
            org_id=ctx.org_id,
            title=body.title,
            description=body.description,
            owner_id=ctx.user_id,
            created_by=ctx.user_id,
        )
        version = await repo.insert_version(
            session, assessment_id=assessment.id, org_id=ctx.org_id, version_no=1, status="draft",
            created_by=ctx.user_id,
        )  # fmt: skip
        await audit.record(
            ctx, session, audit.ASSESSMENT_CREATED, "assessment", assessment.id, after=_snapshot(assessment)
        )
        await audit.record(
            ctx, session, audit.VERSION_CREATED, "assessment_version", version.id,
            after={"assessment_id": assessment.id, "version_no": 1},
        )  # fmt: skip
        return await _detail(session, ctx.org_id, assessment)

    @staticmethod
    async def search(
        org_id: UUID,
        session: AsyncSession,
        params: PageParams,
        *,
        q: str | None,
        status: str | None,
        owner_id: UUID | None,
        linkable: bool,
    ) -> tuple[list[AssessmentListItem], int]:
        sort = parse_sort(params.sort, SORT_FIELDS, default="updated_at:desc")
        rows, total = await repo.list_assessments(
            session, org_id, q=q, status=status, owner_id=owner_id, linkable=linkable, sort=sort,
            offset=params.offset, limit=params.limit,
        )  # fmt: skip
        versions = await repo.list_versions(session, org_id, [r.id for r in rows])
        by_assessment: dict[UUID, list[AssessmentVersion]] = {r.id: [] for r in rows}
        for v in versions:
            by_assessment[v.assessment_id].append(v)
        return [_list_item(r, by_assessment[r.id]) for r in rows], total

    @staticmethod
    async def get(org_id: UUID, session: AsyncSession, assessment_id: UUID) -> AssessmentDetail:
        return await _detail(session, org_id, await _assessment(session, org_id, assessment_id))

    @staticmethod
    async def update(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, body: AssessmentPatch
    ) -> AssessmentDetail:
        assessment = await _assessment(session, ctx.org_id, assessment_id, lock=True)
        before = _snapshot(assessment)
        changes = body.model_dump(exclude_unset=True)
        if "title" in changes and changes["title"] is None:
            raise validation_error([{"field": "title", "code": "null_not_allowed"}])
        for name, value in changes.items():
            setattr(assessment, name, value)
        await repo.flush(session)
        await audit.record(
            ctx,
            session,
            audit.ASSESSMENT_UPDATED,
            "assessment",
            assessment.id,
            before=before,
            after=_snapshot(assessment),
        )
        return await _detail(session, ctx.org_id, assessment)

    @staticmethod
    async def set_archived(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, archived: bool
    ) -> AssessmentDetail:
        assessment = await _assessment(session, ctx.org_id, assessment_id, lock=True)
        before = _snapshot(assessment)
        assessment.status = "archived" if archived else "active"
        await repo.flush(session)
        await audit.record(
            ctx, session, audit.ASSESSMENT_ARCHIVED if archived else audit.ASSESSMENT_UPDATED,
            "assessment", assessment.id, before=before, after=_snapshot(assessment),
        )  # fmt: skip
        return await _detail(session, ctx.org_id, assessment)

    @staticmethod
    async def delete(ctx: TenantContext, session: AsyncSession, assessment_id: UUID) -> None:
        from datetime import UTC, datetime

        assessment = await _assessment(session, ctx.org_id, assessment_id, lock=True)
        roles = await repo.open_role_ids_linked(session, ctx.org_id, assessment.id)
        if roles:
            raise AppError(
                "CONFLICT", details={"reason": "linked_to_open_role", "role_ids": [str(r) for r in roles]}
            )
        before = _snapshot(assessment)
        assessment.deleted_at = datetime.now(UTC)
        await repo.flush(session)
        await audit.record(ctx, session, audit.ASSESSMENT_DELETED, "assessment", assessment.id, before=before)

    @staticmethod
    async def duplicate(ctx: TenantContext, session: AsyncSession, assessment_id: UUID) -> AssessmentDetail:
        source = await _assessment(session, ctx.org_id, assessment_id)
        versions = await repo.list_versions(session, ctx.org_id, [source.id])
        if not versions:
            raise AppError("NOT_FOUND")
        latest = max(versions, key=lambda v: v.version_no)
        copy = await repo.insert_assessment(
            session,
            org_id=ctx.org_id,
            title=f"Copy of {source.title}"[:200],
            description=source.description,
            owner_id=ctx.user_id,
            created_by=ctx.user_id,
        )
        draft = await repo.insert_version(
            session,
            assessment_id=copy.id,
            org_id=ctx.org_id,
            version_no=1,
            status="draft",
            duration_minutes=latest.duration_minutes,
            pass_mark_percent=latest.pass_mark_percent,
            instructions_html=latest.instructions_html,
            header_config=latest.header_config,
            footer_html=latest.footer_html,
            proctoring_config=latest.proctoring_config,
            shuffle_questions=latest.shuffle_questions,
            shuffle_options=latest.shuffle_options,
            created_by=ctx.user_id,
        )
        await repo.clone_content(session, ctx.org_id, latest.id, draft)
        await audit.record(
            ctx, session, audit.ASSESSMENT_DUPLICATED, "assessment", copy.id,
            after={
                **_snapshot(copy),
                "source_assessment_id": source.id,
                "source_version_no": latest.version_no,
            },
        )  # fmt: skip
        return await _detail(session, ctx.org_id, copy)

    # ----- versions

    @staticmethod
    async def list_versions(org_id: UUID, session: AsyncSession, assessment_id: UUID) -> VersionList:
        assessment = await _assessment(session, org_id, assessment_id)
        versions = await repo.list_versions(session, org_id, [assessment.id])
        counts = await repo.question_counts(session, [v.id for v in versions])
        protected = await repo.protected_version_ids(session, org_id, assessment.id)
        return VersionList(items=[_summary(v, counts, protected) for v in versions])

    @staticmethod
    async def get_version(
        org_id: UUID, session: AsyncSession, assessment_id: UUID, version_id: UUID
    ) -> VersionOut:
        return await _version_out(session, org_id, await _version(session, org_id, assessment_id, version_id))

    @staticmethod
    async def create_version(ctx: TenantContext, session: AsyncSession, assessment_id: UUID) -> VersionOut:
        draft = await VersionRules.create_draft(ctx, session, assessment_id)
        return await _version_out(session, ctx.org_id, draft)

    @staticmethod
    async def update_version(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, version_id: UUID, body: VersionPatch
    ) -> VersionOut:
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        changes = body.model_dump(exclude_unset=True)
        before = {name: getattr(version, name) for name in changes}
        if "header_config" in changes:
            logo = body.header_config.logo_asset_id if body.header_config else None
            if logo is not None and not await repo.logo_asset_exists(session, ctx.org_id, logo):
                raise AppError("NOT_FOUND")
            changes["header_config"] = body.header_config.model_dump(mode="json", exclude_none=True)
        if "proctoring_config" in changes:
            changes["proctoring_config"] = body.proctoring_config.model_dump(mode="json")
        for name in ("instructions_html", "footer_html"):
            if name in changes:
                changes[name] = sanitize_html(changes[name])
        for name, value in changes.items():
            setattr(version, name, value)
        await repo.flush(session)
        await audit.record(
            ctx, session, audit.ASSESSMENT_UPDATED, "assessment_version", version.id,
            before=before, after={name: getattr(version, name) for name in changes},
        )  # fmt: skip
        return await _version_out(session, ctx.org_id, version)

    @staticmethod
    async def publish_version(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, version_id: UUID
    ) -> VersionOut:
        """Validate (spec M5 "Publish validations") then freeze. Published rows are immutable in the
        service (here) and in the database (trigger)."""
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        questions = await repo.list_questions(session, ctx.org_id, version.id)
        options = await repo.options_by_question(session, [q.id for q in questions])
        issues = publish_issues(
            [
                PublishView(
                    q.id, q.position, q.type, q.marks, q.scoring_mode, q.answer_key, q.model_answer,
                    [o.is_correct for o in options[q.id]],
                )
                for q in questions
            ]
        )  # fmt: skip
        if issues:
            only_model_answer = all(i["code"] == "model_answer_required" for i in issues)
            raise AppError(
                "MODEL_ANSWER_REQUIRED" if only_model_answer else "QUESTION_INVALID",
                details={"issues": issues},
            )
        fields = []
        if not 5 <= version.duration_minutes <= 480:
            fields.append({"field": "duration_minutes", "code": "out_of_range"})
        if not 0 <= version.pass_mark_percent <= 100:
            fields.append({"field": "pass_mark_percent", "code": "out_of_range"})
        if fields:
            raise validation_error(fields)
        total = sum((q.marks for q in questions), Decimal("0"))
        await repo.publish_version(session, version, total, ctx.user_id)
        await audit.record(
            ctx, session, audit.VERSION_PUBLISHED, "assessment_version", version.id,
            after={"assessment_id": assessment_id, "version_no": version.version_no,
                   "questions": len(questions), "total_marks": total},
        )  # fmt: skip
        return await _version_out(session, ctx.org_id, version)

    # ----- questions

    @staticmethod
    async def add_question(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, version_id: UUID, body: QuestionIn
    ) -> QuestionOut:
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        created = await AssessmentService._insert(ctx, session, version, body)
        await repo.recompute_total(session, version)
        return created

    @staticmethod
    async def bulk_add(
        ctx: TenantContext,
        session: AsyncSession,
        assessment_id: UUID,
        version_id: UUID,
        body: BulkQuestionsIn,
    ) -> QuestionList:
        """Validate every item first, then insert all (one failing suggestion rejects the batch)."""
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        normalized = []
        for index, item in enumerate(body.questions):
            try:
                normalized.append(normalize_question(item))
            except AppError as exc:
                details = {**(exc.details or {}), "index": index}
                raise AppError(exc.code, details=details) from exc
        position = await repo.next_position(session, version.id)
        created = []
        for data in normalized:
            created.append(await AssessmentService._insert(ctx, session, version, data, position=position))
            position += 1
        await repo.recompute_total(session, version)
        return QuestionList(items=created)

    @staticmethod
    async def _insert(
        ctx: TenantContext,
        session: AsyncSession,
        version: AssessmentVersion,
        body: Any,
        *,
        position: int | None = None,
    ) -> QuestionOut:
        data = body if not isinstance(body, QuestionIn) else normalize_question(body)
        position = position or await repo.next_position(session, version.id)
        question = await repo.insert_question(session, ctx.org_id, version.id, position, data)
        await audit.record(
            ctx, session, audit.QUESTION_ADDED, "question", question.id,
            after={"version_id": version.id, "position": position, "type": data.type, "marks": data.marks},
        )  # fmt: skip
        options = await repo.options_by_question(session, [question.id])
        return _question_out(question, options[question.id])

    @staticmethod
    async def update_question(
        ctx: TenantContext,
        session: AsyncSession,
        assessment_id: UUID,
        version_id: UUID,
        question_id: UUID,
        body: QuestionPatch,
    ) -> QuestionOut:
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        question = await repo.get_question(session, ctx.org_id, version.id, question_id)
        if question is None:
            raise AppError("NOT_FOUND")
        options = (await repo.options_by_question(session, [question.id]))[question.id]
        current = _question_out(question, options)
        merged: dict[str, Any] = {
            "type": current.type,
            "prompt_html": current.prompt_html,
            "marks": current.marks,
            "scoring_mode": current.scoring_mode,
            "options": [{"text_html": o.text_html, "is_correct": o.is_correct} for o in current.options],
            "answer_key": current.answer_key,
            "model_answer": current.model_answer,
            "code_language": current.code_language,
        }
        changes = body.model_dump(exclude_unset=True)
        merged.update(changes)
        if "scoring_mode" not in changes and merged["scoring_mode"] not in ALLOWED_SCORING[merged["type"]]:
            merged["scoring_mode"] = DEFAULT_SCORING[merged["type"]]
        if "type" in changes and changes["type"] != current.type:
            merged.setdefault("answer_key", None)
            if "answer_key" not in changes:
                merged["answer_key"] = None
            if "options" not in changes and merged["type"] not in ("single_choice", "multi_choice"):
                merged["options"] = []
        if "options" in changes and body.options is not None:
            merged["options"] = [o.model_dump() for o in body.options]
        data = normalize_question(QuestionIn.model_validate(merged))
        before = {"type": question.type, "marks": question.marks, "scoring_mode": question.scoring_mode}
        await repo.apply_question(session, question, data)
        await repo.recompute_total(session, version)
        await audit.record(
            ctx, session, audit.QUESTION_UPDATED, "question", question.id, before=before,
            after={"type": data.type, "marks": data.marks, "scoring_mode": data.scoring_mode},
        )  # fmt: skip
        options = (await repo.options_by_question(session, [question.id]))[question.id]
        return _question_out(question, options)

    @staticmethod
    async def delete_question(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, version_id: UUID, question_id: UUID
    ) -> None:
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        question = await repo.get_question(session, ctx.org_id, version.id, question_id)
        if question is None:
            raise AppError("NOT_FOUND")
        before = {"version_id": version.id, "position": question.position, "type": question.type}
        await repo.delete_question(session, question)
        remaining = await repo.list_questions(session, ctx.org_id, version.id)
        await repo.renumber(session, remaining)
        await repo.recompute_total(session, version)
        await audit.record(ctx, session, audit.QUESTION_DELETED, "question", question_id, before=before)

    @staticmethod
    async def reorder(
        ctx: TenantContext, session: AsyncSession, assessment_id: UUID, version_id: UUID, ids: list[UUID]
    ) -> QuestionList:
        version = await _draft(session, ctx.org_id, assessment_id, version_id)
        questions = await repo.list_questions(session, ctx.org_id, version.id)
        by_id = {q.id: q for q in questions}
        if len(ids) != len(set(ids)) or set(ids) != set(by_id):
            raise validation_error([{"field": "question_ids", "code": "must_list_every_question_once"}])
        await repo.renumber(session, [by_id[i] for i in ids])
        await audit.record(
            ctx, session, audit.QUESTIONS_REORDERED, "assessment_version", version.id,
            after={"question_ids": ids},
        )  # fmt: skip
        return QuestionList(items=await _questions_out(session, ctx.org_id, version.id))

    # ----- preview

    @staticmethod
    async def preview(
        org_id: UUID, session: AsyncSession, assessment_id: UUID, version_id: UUID
    ) -> PreviewOut:
        """Exactly what a candidate will see (draft or published): header, instructions, footer,
        watermark (only while the SA's ``watermark`` flag is on for the org) and questions without keys."""
        version = await _version(session, org_id, assessment_id, version_id)
        header = version.header_config or {}
        show_watermark = await flags.is_enabled(session, org_id, flags.WATERMARK)
        return PreviewOut(
            version_id=version.id,
            version_no=version.version_no,
            status=version.status,
            header=PreviewHeader(
                company_name=header.get("company_name"), logo_asset_id=header.get("logo_asset_id")
            ),
            instructions_html=version.instructions_html,
            footer_html=version.footer_html,
            watermark=watermark() if show_watermark else None,
            duration_minutes=version.duration_minutes,
            shuffle_questions=version.shuffle_questions,
            shuffle_options=version.shuffle_options,
            total_marks=version.total_marks,
            questions=candidate_questions(await _gradable(session, org_id, version.id)),
        )
