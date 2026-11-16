"""M5 data access. Every query filters ``org_id`` (RLS is the second layer).

Tables owned by other modules (roles, links, invites, attempts, assets) are read with ``text()``
SQL, never through their models.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db import Assessment, AssessmentVersion, Question, QuestionOption
from app.modules.m05_assessments.question_rules import NormalizedQuestion

# --------------------------------------------------------------------------- flush with error mapping


async def flush(session: AsyncSession) -> None:
    """Flush, mapping the database's second-layer guards to their error codes."""
    try:
        await session.flush()
    except DBAPIError as exc:
        message = str(exc.orig)
        if "ASSESSMENT_IMMUTABLE" in message:
            raise AppError("ASSESSMENT_IMMUTABLE") from exc
        if isinstance(exc, IntegrityError) and "uq_assessment_versions_one_draft" in message:
            raise AppError("DRAFT_ALREADY_EXISTS") from exc
        raise


# --------------------------------------------------------------------------- assessments


async def get_assessment(
    session: AsyncSession, org_id: UUID, assessment_id: UUID, *, lock: bool = False
) -> Assessment | None:
    stmt = select(Assessment).where(
        Assessment.id == assessment_id, Assessment.org_id == org_id, Assessment.deleted_at.is_(None)
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def owner_id(session: AsyncSession, org_id: UUID, assessment_id: UUID) -> UUID | None:
    stmt = select(Assessment.owner_id).where(
        Assessment.id == assessment_id, Assessment.org_id == org_id, Assessment.deleted_at.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_assessment(session: AsyncSession, **values: Any) -> Assessment:
    row = Assessment(**values)
    session.add(row)
    await flush(session)
    return row


async def list_assessments(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None,
    status: str | None,
    owner_id: UUID | None,
    linkable: bool,
    sort: list[tuple[str, str]],
    offset: int,
    limit: int,
) -> tuple[list[Assessment], int]:
    conditions = [Assessment.org_id == org_id, Assessment.deleted_at.is_(None)]
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Assessment.title.ilike(f"%{escaped}%", escape="\\"))
    if status:
        conditions.append(Assessment.status == status)
    if owner_id:
        conditions.append(Assessment.owner_id == owner_id)
    if linkable:
        published = (
            select(AssessmentVersion.id)
            .where(AssessmentVersion.assessment_id == Assessment.id, AssessmentVersion.status == "published")
            .exists()
        )
        conditions += [published, Assessment.status == "active"]
    columns = {"title": Assessment.title, "created_at": Assessment.created_at, "updated_at": Assessment.updated_at}
    order = [columns[f].asc() if d == "asc" else columns[f].desc() for f, d in sort] + [Assessment.id]
    total = (await session.execute(select(func.count()).select_from(Assessment).where(*conditions))).scalar_one()
    rows = (
        (await session.execute(select(Assessment).where(*conditions).order_by(*order).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return list(rows), total


# --------------------------------------------------------------------------- versions


async def get_version(
    session: AsyncSession, org_id: UUID, assessment_id: UUID, version_id: UUID, *, lock: bool = False
) -> AssessmentVersion | None:
    stmt = select(AssessmentVersion).where(
        AssessmentVersion.id == version_id,
        AssessmentVersion.assessment_id == assessment_id,
        AssessmentVersion.org_id == org_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_version_any(session: AsyncSession, org_id: UUID, version_id: UUID) -> AssessmentVersion | None:
    stmt = select(AssessmentVersion).where(AssessmentVersion.id == version_id, AssessmentVersion.org_id == org_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_versions(
    session: AsyncSession, org_id: UUID, assessment_ids: list[UUID]
) -> list[AssessmentVersion]:
    if not assessment_ids:
        return []
    stmt = (
        select(AssessmentVersion)
        .where(AssessmentVersion.org_id == org_id, AssessmentVersion.assessment_id.in_(assessment_ids))
        .order_by(AssessmentVersion.version_no.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def next_version_no(session: AsyncSession, assessment_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(AssessmentVersion.version_no), 0)).where(
        AssessmentVersion.assessment_id == assessment_id
    )
    return (await session.execute(stmt)).scalar_one() + 1


async def insert_version(session: AsyncSession, **values: Any) -> AssessmentVersion:
    row = AssessmentVersion(**values)
    session.add(row)
    await flush(session)
    return row


async def question_counts(session: AsyncSession, version_ids: list[UUID]) -> dict[UUID, int]:
    if not version_ids:
        return {}
    stmt = select(Question.version_id, func.count()).where(Question.version_id.in_(version_ids)).group_by(
        Question.version_id
    )
    return {vid: n for vid, n in (await session.execute(stmt)).all()}


async def recompute_total(session: AsyncSession, version: AssessmentVersion) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Question.marks), 0)).where(Question.version_id == version.id)
        )
    ).scalar_one()
    version.total_marks = Decimal(total)
    await flush(session)
    return version.total_marks


async def publish_version(session: AsyncSession, version: AssessmentVersion, total: Decimal, user_id: UUID) -> None:
    version.status = "published"
    version.total_marks = total
    version.published_at = datetime.now(UTC)
    version.published_by = user_id
    await flush(session)


async def delete_version(session: AsyncSession, version: AssessmentVersion) -> None:
    """The 3-version purge. Questions, options, answers and links cascade; attempts and invites keep
    their rows (version reference set to NULL by the schema)."""
    await session.delete(version)
    await flush(session)


# --------------------------------------------------------------------------- questions


async def get_question(session: AsyncSession, org_id: UUID, version_id: UUID, question_id: UUID) -> Question | None:
    stmt = select(Question).where(
        Question.id == question_id, Question.version_id == version_id, Question.org_id == org_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_questions(session: AsyncSession, org_id: UUID, version_id: UUID) -> list[Question]:
    stmt = (
        select(Question)
        .where(Question.version_id == version_id, Question.org_id == org_id)
        .order_by(Question.position, Question.created_at, Question.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def options_by_question(session: AsyncSession, question_ids: list[UUID]) -> dict[UUID, list[QuestionOption]]:
    grouped: dict[UUID, list[QuestionOption]] = {qid: [] for qid in question_ids}
    if question_ids:
        stmt = (
            select(QuestionOption)
            .where(QuestionOption.question_id.in_(question_ids))
            .order_by(QuestionOption.position, QuestionOption.id)
        )
        for option in (await session.execute(stmt)).scalars():
            grouped[option.question_id].append(option)
    return grouped


async def next_position(session: AsyncSession, version_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(Question.position), 0)).where(Question.version_id == version_id)
    return (await session.execute(stmt)).scalar_one() + 1


async def insert_question(
    session: AsyncSession, org_id: UUID, version_id: UUID, position: int, data: NormalizedQuestion
) -> Question:
    question = Question(
        id=uuid4(),
        version_id=version_id,
        org_id=org_id,
        position=position,
        type=data.type,
        prompt_html=data.prompt_html,
        marks=data.marks,
        scoring_mode=data.scoring_mode,
        answer_key=data.answer_key,
        model_answer=data.model_answer,
        code_language=data.code_language,
    )
    session.add(question)
    await flush(session)
    await add_options(session, question.id, data.options)
    return question


async def add_options(session: AsyncSession, question_id: UUID, options: list[tuple[str, bool]]) -> None:
    session.add_all(
        QuestionOption(question_id=question_id, position=i, text_html=html, is_correct=correct)
        for i, (html, correct) in enumerate(options, start=1)
    )
    await flush(session)


async def apply_question(session: AsyncSession, question: Question, data: NormalizedQuestion) -> None:
    question.type = data.type
    question.prompt_html = data.prompt_html
    question.marks = data.marks
    question.scoring_mode = data.scoring_mode
    question.answer_key = data.answer_key
    question.model_answer = data.model_answer
    question.code_language = data.code_language
    await flush(session)
    await session.execute(
        text("DELETE FROM question_options WHERE question_id = :q"), {"q": question.id}
    )
    await add_options(session, question.id, data.options)


async def delete_question(session: AsyncSession, question: Question) -> None:
    await session.delete(question)
    await flush(session)


async def renumber(session: AsyncSession, ordered: list[Question]) -> None:
    """Positions become 1..n in the given order."""
    for position, question in enumerate(ordered, start=1):
        question.position = position
    await flush(session)


async def clone_content(session: AsyncSession, org_id: UUID, source_id: UUID, target: AssessmentVersion) -> None:
    """Copy every question (with options) of ``source_id`` into the draft ``target``."""
    questions = await list_questions(session, org_id, source_id)
    options = await options_by_question(session, [q.id for q in questions])
    for q in questions:
        copy = Question(
            id=uuid4(),
            version_id=target.id,
            org_id=org_id,
            position=q.position,
            type=q.type,
            prompt_html=q.prompt_html,
            marks=q.marks,
            scoring_mode=q.scoring_mode,
            answer_key=q.answer_key,
            model_answer=q.model_answer,
            code_language=q.code_language,
        )
        session.add(copy)
        await flush(session)
        await add_options(session, copy.id, [(o.text_html, o.is_correct) for o in options[q.id]])
    await recompute_total(session, target)


# --------------------------------------------------------------------------- reads on other modules' tables

_PROTECTED_SQL = text(
    """
    SELECT v.id FROM assessment_versions v
    WHERE v.org_id = :org_id AND v.assessment_id = :assessment_id AND (
        EXISTS (SELECT 1 FROM role_assessment_links l JOIN open_roles r ON r.id = l.role_id
                WHERE l.assessment_version_id = v.id AND l.unlinked_at IS NULL
                  AND r.status <> 'closed' AND r.deleted_at IS NULL)
        OR EXISTS (SELECT 1 FROM attempts a
                   WHERE a.assessment_version_id = v.id AND a.decision = 'waiting' AND a.purged_at IS NULL)
        OR EXISTS (SELECT 1 FROM invites i
                   WHERE i.assessment_version_id = v.id AND i.status IN ('invited', 'started'))
    )
    """
)


async def protected_version_ids(session: AsyncSession, org_id: UUID, assessment_id: UUID) -> set[UUID]:
    """Versions that are "in use" (spec 5.4): linked to a non-closed role, or with an attempt not yet
    decided, or with a pending invite (a replaced link keeps pending invites on the old version, S-08)."""
    rows = await session.execute(_PROTECTED_SQL, {"org_id": org_id, "assessment_id": assessment_id})
    return {row[0] for row in rows}


_OPEN_ROLES_SQL = text(
    """
    SELECT DISTINCT r.id FROM role_assessment_links l
    JOIN assessment_versions v ON v.id = l.assessment_version_id
    JOIN open_roles r ON r.id = l.role_id
    WHERE v.assessment_id = :assessment_id AND l.org_id = :org_id AND l.unlinked_at IS NULL
      AND r.status <> 'closed' AND r.deleted_at IS NULL
    ORDER BY r.id
    """
)


async def open_role_ids_linked(session: AsyncSession, org_id: UUID, assessment_id: UUID) -> list[UUID]:
    rows = await session.execute(_OPEN_ROLES_SQL, {"org_id": org_id, "assessment_id": assessment_id})
    return [row[0] for row in rows]


async def logo_asset_exists(session: AsyncSession, org_id: UUID, asset_id: UUID) -> bool:
    row = await session.execute(
        text("SELECT 1 FROM assets WHERE id = :id AND org_id = :org_id AND kind = 'logo'"),
        {"id": asset_id, "org_id": org_id},
    )
    return row.first() is not None
