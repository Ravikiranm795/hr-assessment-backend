"""Integration test of ``core.audit.record`` (Task 2) against the real ``audit_logs`` table."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core import db as core_db
from tests import factories as f


def _ctx(org_id, user_id, role="org_admin"):
    return core_db.TenantContext(
        org_id=org_id,
        user_id=user_id,
        role=role,
        is_platform=False,
        request_id="req-audit-1",
        ip="203.0.113.7",
        user_agent="pytest-agent",
        actor_name="Ada Admin",
    )


async def _row(session: AsyncSession, entity_id: str):
    return (
        (await session.execute(text("SELECT * FROM audit_logs WHERE entity_id = :e"), {"e": entity_id}))
        .mappings()
        .one()
    )


async def test_record_writes_row_with_context_and_redaction(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    admin = await f.make_user(db_session, "org_admin", org)
    await db_session.commit()
    target = uuid.uuid4()

    await audit.record(
        _ctx(org.id, admin.id),
        db_session,
        "USER_UPDATED",
        "user",
        target,
        before={"name": "Old", "password_hash": "secret-hash"},
        after={"name": "New", "password_hash": "other-hash", "api_key": "sk-123"},
        reason="typo",
    )
    await db_session.commit()

    row = await _row(db_session, str(target))
    assert row["org_id"] == org.id
    assert row["actor_user_id"] == admin.id
    assert row["actor_role"] == "org_admin"
    assert row["actor_type"] == "user"
    assert row["actor_name_snapshot"] == "Ada Admin"
    assert row["action"] == "USER_UPDATED"
    assert row["entity_type"] == "user"
    assert row["reason"] == "typo"
    assert row["result"] == "success"
    assert row["ip"] == "203.0.113.7"
    assert row["user_agent"] == "pytest-agent"
    assert row["request_id"] == "req-audit-1"
    assert row["id"].version == 7
    assert row["before"]["name"] == "Old" and row["after"]["name"] == "New"
    for blob in (row["before"], row["after"]):
        assert "secret-hash" not in str(blob) and "other-hash" not in str(blob) and "sk-123" not in str(blob)


async def test_record_rolls_back_with_the_change(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    await db_session.commit()
    target = uuid.uuid4()
    await audit.record(_ctx(org.id, None, role="system"), db_session, "ORG_UPDATED", "organization", target)
    await db_session.rollback()
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE entity_id = :e"), {"e": str(target)}
        )
    ).scalar()
    assert count == 0
