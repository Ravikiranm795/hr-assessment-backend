"""M9 data access. Every query filters ``org_id`` (RLS is the second layer). Invites, roles, assets and
help requests belong to other modules and are read with ``text()`` SQL."""

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Attempt, AttemptAnswer, Decision, ProctorEvent


async def get_attempt(
    session: AsyncSession, org_id: UUID, attempt_id: UUID, *, lock: bool = False
) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.id == attempt_id, Attempt.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def attempt_owner(session: AsyncSession, org_id: UUID, attempt_id: UUID) -> UUID | None:
    row = await session.execute(
        text(
            "SELECT r.owner_id FROM attempts a JOIN invites i ON i.id = a.invite_id "
            "JOIN open_roles r ON r.id = i.role_id WHERE a.id = :a AND a.org_id = :o"
        ),
        {"a": attempt_id, "o": org_id},
    )
    return row.scalar_one_or_none()


async def role_owner(session: AsyncSession, org_id: UUID, role_id: UUID) -> UUID | None:
    row = await session.execute(
        text("SELECT owner_id FROM open_roles WHERE id = :r AND org_id = :o AND deleted_at IS NULL"),
        {"r": role_id, "o": org_id},
    )
    return row.scalar_one_or_none()


async def invite_for(session: AsyncSession, org_id: UUID, invite_id: UUID) -> dict[str, Any]:
    row = await session.execute(
        text("SELECT * FROM invites WHERE id = :i AND org_id = :o"), {"i": invite_id, "o": org_id}
    )
    return dict(row.one()._mapping)


async def list_role_attempts(
    session: AsyncSession, org_id: UUID, role_id: UUID, *, offset: int, limit: int
) -> tuple[list[dict], int]:
    base = "FROM attempts a JOIN invites i ON i.id = a.invite_id WHERE a.org_id = :o AND i.role_id = :r"
    p = {"o": org_id, "r": role_id, "off": offset, "lim": limit}
    total = (await session.execute(text(f"SELECT count(*) {base}"), p)).scalar_one()
    rows = await session.execute(
        text(
            "SELECT a.id, a.invite_id, i.candidate_name, i.candidate_email_enc, a.status, a.scoring_status, "
            "a.score_percent, a.passed, a.decision, a.flag_counts, a.submitted_at "
            f"{base} ORDER BY a.submitted_at DESC NULLS LAST, a.created_at DESC, a.id OFFSET :off LIMIT :lim"
        ),
        p,
    )
    return [dict(r._mapping) for r in rows], total


async def answers(session: AsyncSession, attempt_id: UUID) -> list[AttemptAnswer]:
    rows = await session.execute(select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id))
    return list(rows.scalars())


async def events(session: AsyncSession, org_id: UUID, attempt_id: UUID) -> list[ProctorEvent]:
    stmt = select(ProctorEvent).where(ProctorEvent.org_id == org_id, ProctorEvent.attempt_id == attempt_id)
    return list((await session.execute(stmt.order_by(ProctorEvent.received_at, ProctorEvent.id))).scalars())


async def get_event(
    session: AsyncSession, org_id: UUID, attempt_id: UUID, event_id: UUID
) -> ProctorEvent | None:
    stmt = select(ProctorEvent).where(
        ProctorEvent.id == event_id, ProctorEvent.org_id == org_id, ProctorEvent.attempt_id == attempt_id
    )
    return (await session.execute(stmt.with_for_update())).scalar_one_or_none()


async def decisions(session: AsyncSession, org_id: UUID, attempt_id: UUID) -> list[Decision]:
    stmt = select(Decision).where(Decision.org_id == org_id, Decision.attempt_id == attempt_id)
    return list((await session.execute(stmt.order_by(Decision.created_at, Decision.id))).scalars())


async def help_requests(session: AsyncSession, org_id: UUID, invite_id: UUID) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT id, reason, note, status, created_at, decided_at, decision_note, granted_camera_exempt, "
            "granted_extra_minutes FROM help_requests "
            "WHERE org_id = :o AND invite_id = :i ORDER BY created_at"
        ),
        {"o": org_id, "i": invite_id},
    )
    return [dict(r._mapping) for r in rows]


async def asset_keys(session: AsyncSession, org_id: UUID, ids: list[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        text("SELECT id, storage_key FROM assets WHERE org_id = :o AND id = ANY(:ids)"),
        {"o": org_id, "ids": ids},
    )
    return {r.id: r.storage_key for r in rows}


async def delete_assets(session: AsyncSession, org_id: UUID, ids: list[UUID]) -> None:
    if ids:
        await session.execute(
            text("DELETE FROM assets WHERE org_id = :o AND id = ANY(:ids)"), {"o": org_id, "ids": ids}
        )


async def delete_answers(session: AsyncSession, attempt_id: UUID) -> int:
    result = await session.execute(delete(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id))
    return result.rowcount or 0


async def delete_events(session: AsyncSession, org_id: UUID, attempt_id: UUID) -> int:
    result = await session.execute(
        delete(ProctorEvent).where(ProctorEvent.org_id == org_id, ProctorEvent.attempt_id == attempt_id)
    )
    return result.rowcount or 0


async def count_decisions(session: AsyncSession, attempt_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Decision).where(Decision.attempt_id == attempt_id)
        )
    ).scalar_one()
