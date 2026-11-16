"""M8 data access. Every query filters ``org_id`` (RLS is the second layer); ``open`` runs in platform
mode and looks the invite up by token hash first, then works org-scoped."""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Asset, Attempt, AttemptAnswer, HelpRequest, Invite, ProctorEvent


async def invite_by_token_hash(session: AsyncSession, token_hash: str) -> Invite | None:
    stmt = select(Invite).where(Invite.token_hash == token_hash).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_invite(
    session: AsyncSession, org_id: UUID, invite_id: UUID, *, lock: bool = False
) -> Invite | None:
    stmt = select(Invite).where(Invite.id == invite_id, Invite.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_attempt(
    session: AsyncSession, org_id: UUID, attempt_id: UUID, *, lock: bool = False
) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.id == attempt_id, Attempt.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def attempt_for_invite(session: AsyncSession, org_id: UUID, invite_id: UUID) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.org_id == org_id, Attempt.invite_id == invite_id)
    return (await session.execute(stmt.order_by(Attempt.created_at).limit(1))).scalar_one_or_none()


async def org_status(session: AsyncSession, org_id: UUID) -> str | None:
    row = await session.execute(text("SELECT status FROM organizations WHERE id = :o"), {"o": org_id})
    return row.scalar_one_or_none()


async def role_info(session: AsyncSession, org_id: UUID, role_id: UUID) -> dict | None:
    row = await session.execute(
        text(
            "SELECT status, title, owner_id FROM open_roles "
            "WHERE id = :r AND org_id = :o AND deleted_at IS NULL"
        ),
        {"r": role_id, "o": org_id},
    )
    found = row.first()
    return dict(found._mapping) if found else None


async def add_asset(session: AsyncSession, asset: Asset) -> Asset:
    session.add(asset)
    await session.flush()
    return asset


# --------------------------------------------------------------------------- answers and events


async def upsert_answer(session: AsyncSession, attempt_id: UUID, question_id: UUID, answer) -> None:
    from sqlalchemy.dialects.postgresql import insert

    stmt = insert(AttemptAnswer).values(attempt_id=attempt_id, question_id=question_id, answer=answer)
    stmt = stmt.on_conflict_do_update(
        index_elements=["attempt_id", "question_id"],
        set_={"answer": stmt.excluded.answer, "updated_at": text("now()")},
    )
    await session.execute(stmt)


async def answers_map(session: AsyncSession, attempt_id: UUID) -> dict[str, object]:
    rows = await session.execute(
        select(AttemptAnswer.question_id, AttemptAnswer.answer).where(AttemptAnswer.attempt_id == attempt_id)
    )
    return {str(q): a for q, a in rows}


async def count_tab_events(session: AsyncSession, attempt_id: UUID) -> int:
    row = await session.execute(
        text(
            "SELECT count(*) FROM proctor_events "
            "WHERE attempt_id = :a AND type IN ('tab_switch', 'window_blur')"
        ),
        {"a": attempt_id},
    )
    return row.scalar_one()


async def add_event(session: AsyncSession, event: ProctorEvent) -> ProctorEvent:
    session.add(event)
    await session.flush()
    return event


async def get_event(
    session: AsyncSession, org_id: UUID, attempt_id: UUID, event_id: UUID
) -> ProctorEvent | None:
    stmt = select(ProctorEvent).where(
        ProctorEvent.id == event_id, ProctorEvent.org_id == org_id, ProctorEvent.attempt_id == attempt_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- help requests


async def open_help_request(session: AsyncSession, org_id: UUID, invite_id: UUID) -> HelpRequest | None:
    stmt = select(HelpRequest).where(
        HelpRequest.org_id == org_id, HelpRequest.invite_id == invite_id, HelpRequest.status == "open"
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_help_request(session: AsyncSession, org_id: UUID, invite_id: UUID) -> HelpRequest | None:
    stmt = select(HelpRequest).where(HelpRequest.org_id == org_id, HelpRequest.invite_id == invite_id)
    return (await session.execute(stmt.order_by(HelpRequest.created_at.desc()).limit(1))).scalar_one_or_none()
