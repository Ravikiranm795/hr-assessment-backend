"""Database guards: triggers (published immutability, append-only audit, protected
default SA), partial unique indexes and the maintenance partition functions."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import HelpRequest, Question, RoleAssessmentLink
from tests import factories as f


async def _exec(engine: AsyncEngine, sql: str, role: str | None = None, **params):
    async with engine.connect() as conn:
        async with conn.begin():
            if role:
                await conn.execute(text(f"SET LOCAL ROLE {role}"))
                await conn.execute(text("SELECT set_config('app.is_platform', 'on', true)"))
            return await conn.execute(text(sql), params)


# --------------------------------------------------------------------------- audit_logs


async def _audit_row(engine: AsyncEngine) -> uuid.UUID:
    result = await _exec(
        engine,
        "INSERT INTO audit_logs (actor_type, action) VALUES ('system', 'TEST_EVENT') RETURNING id",
        role="app_rw",
    )
    return result.scalar()


async def test_audit_insert_and_select_allowed_for_app_role(db_engine: AsyncEngine) -> None:
    row_id = await _audit_row(db_engine)
    found = await _exec(db_engine, "SELECT count(*) FROM audit_logs WHERE id = :id", role="app_rw", id=row_id)
    assert found.scalar() == 1


async def test_audit_update_and_delete_rejected(db_engine: AsyncEngine) -> None:
    row_id = await _audit_row(db_engine)
    # Superuser: grants do not apply, the trigger does.
    with pytest.raises(DBAPIError, match="AUDIT_APPEND_ONLY"):
        await _exec(db_engine, "UPDATE audit_logs SET action = 'X' WHERE id = :id", id=row_id)
    with pytest.raises(DBAPIError, match="AUDIT_APPEND_ONLY"):
        await _exec(db_engine, "DELETE FROM audit_logs WHERE id = :id", id=row_id)
    with pytest.raises(DBAPIError, match="AUDIT_APPEND_ONLY"):
        await _exec(db_engine, "TRUNCATE audit_logs")
    # Application role: no UPDATE or DELETE privilege at all.
    with pytest.raises(DBAPIError, match="permission denied"):
        await _exec(db_engine, "UPDATE audit_logs SET action = 'X' WHERE id = :id", role="app_rw", id=row_id)
    with pytest.raises(DBAPIError, match="permission denied"):
        await _exec(db_engine, "DELETE FROM audit_logs WHERE id = :id", role="app_rw", id=row_id)


async def test_audit_partition_rejects_direct_update(db_engine: AsyncEngine) -> None:
    """The trigger also guards each partition (row triggers clone to partitions)."""
    row_id = await _audit_row(db_engine)
    month = datetime.now(UTC)
    partition = f"audit_logs_y{month.year:04d}m{month.month:02d}"
    with pytest.raises(DBAPIError, match="AUDIT_APPEND_ONLY"):
        await _exec(db_engine, f"UPDATE {partition} SET action = 'X' WHERE id = :id", id=row_id)
    with pytest.raises(DBAPIError, match="permission denied"):
        await _exec(db_engine, f"SELECT count(*) FROM {partition}", role="app_rw")


async def test_audit_id_defaults_to_uuid_v7(db_engine: AsyncEngine) -> None:
    row_id = await _audit_row(db_engine)
    assert row_id.version == 7


async def test_maintenance_role_ensures_and_drops_partitions(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET LOCAL ROLE maintenance"))
            created = (
                await conn.execute(text("SELECT audit_ensure_partitions(2, DATE '2020-01-01')"))
            ).scalar()
            assert created == 2
            dropped = (
                await conn.execute(text("SELECT audit_drop_partitions_before(DATE '2020-02-01')"))
            ).scalar()
            assert dropped == 1
        names = set(
            (
                await conn.execute(
                    text(
                        "SELECT relname FROM pg_class WHERE relkind = 'r' "
                        "AND relname LIKE 'audit_logs_y2020%'"
                    )
                )
            ).scalars()
        )
    assert names == {"audit_logs_y2020m02"}
    for fn in ("audit_ensure_partitions(1)", "audit_drop_partitions_before(DATE '2020-03-01')"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _exec(db_engine, f"SELECT {fn}", role="app_rw")
    await _exec(db_engine, "SELECT audit_drop_partitions_before(DATE '2020-03-01')")


# --------------------------------------------------------------------------- published versions


@pytest.fixture
async def published(db_session: AsyncSession):
    org = await f.make_org(db_session)
    assessment = await f.make_assessment(db_session, org)
    version = await f.make_version(db_session, assessment, status="published", questions=2)
    await db_session.commit()
    question_id = (
        await db_session.execute(
            text("SELECT id FROM questions WHERE version_id = :v LIMIT 1"), {"v": version.id}
        )
    ).scalar()
    return {"org": org, "assessment": assessment, "version": version, "question_id": question_id}


async def test_published_version_update_rejected(db_engine: AsyncEngine, published) -> None:
    version_id = published["version"].id
    with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
        await _exec(
            db_engine, "UPDATE assessment_versions SET duration_minutes = 5 WHERE id = :id", id=version_id
        )
    with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
        await _exec(
            db_engine,
            "UPDATE assessment_versions SET status = 'draft' WHERE id = :id",
            role="app_rw",
            id=version_id,
        )


async def test_published_version_questions_and_options_immutable(db_engine: AsyncEngine, published) -> None:
    qid, vid, org = published["question_id"], published["version"].id, published["org"]
    for sql in (
        "UPDATE questions SET marks = 9 WHERE id = :q",
        "DELETE FROM questions WHERE id = :q",
        "UPDATE question_options SET is_correct = NOT is_correct WHERE question_id = :q",
        "DELETE FROM question_options WHERE question_id = :q",
        "INSERT INTO question_options (question_id, position, text_html) VALUES (:q, 9, 'new')",
    ):
        with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
            await _exec(db_engine, sql, role="app_rw", q=qid)
    with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
        await _exec(
            db_engine,
            "INSERT INTO questions (version_id, org_id, position, type, prompt_html) "
            "VALUES (:v, :o, 9, 'short_answer', 'p')",
            role="app_rw",
            v=vid,
            o=org.id,
        )


async def test_question_cannot_move_into_published_version(db_session: AsyncSession, published) -> None:
    draft = await f.make_version(db_session, published["assessment"], status="draft", version_no=2)
    await db_session.commit()
    with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
        await db_session.execute(
            text("UPDATE questions SET version_id = :p WHERE version_id = :d"),
            {"p": published["version"].id, "d": draft.id},
        )
    await db_session.rollback()


async def test_draft_version_and_its_questions_are_editable(db_session: AsyncSession, published) -> None:
    draft = await f.make_version(db_session, published["assessment"], status="draft", version_no=2)
    draft.duration_minutes = 45
    question = (
        await db_session.execute(text("SELECT id FROM questions WHERE version_id = :v"), {"v": draft.id})
    ).scalar()
    await db_session.execute(text("UPDATE questions SET marks = 3 WHERE id = :q"), {"q": question})
    await db_session.execute(text("DELETE FROM question_options WHERE question_id = :q"), {"q": question})
    draft.status = "published"  # publishing a draft is an UPDATE of a draft row
    await db_session.commit()


async def test_published_version_can_be_purged_with_cascade(db_engine: AsyncEngine, published) -> None:
    vid = published["version"].id
    await _exec(db_engine, "DELETE FROM assessment_versions WHERE id = :v", role="app_rw", v=vid)
    left = await _exec(db_engine, "SELECT count(*) FROM questions WHERE version_id = :v", v=vid)
    assert left.scalar() == 0


async def test_one_draft_per_assessment(db_session: AsyncSession, published) -> None:
    await f.make_version(db_session, published["assessment"], status="draft", version_no=2)
    with pytest.raises(IntegrityError, match="uq_assessment_versions_one_draft"):
        await f.make_version(db_session, published["assessment"], status="draft", version_no=3)
    await db_session.rollback()


# --------------------------------------------------------------------------- default super admin


@pytest.fixture
async def default_sa(db_session: AsyncSession):
    user = await f.make_user(db_session, "super_admin", is_default_super_admin=True)
    await db_session.commit()
    user_id = user.id
    yield user
    async with db_session.bind.connect() as conn:  # remove it so the next test can create one
        async with conn.begin():
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def test_default_super_admin_cannot_be_updated_or_deleted(db_engine: AsyncEngine, default_sa) -> None:
    for sql in (
        "UPDATE users SET name = 'renamed' WHERE id = :id",
        "UPDATE users SET status = 'inactive' WHERE id = :id",
        "UPDATE users SET email = 'other@example.com' WHERE id = :id",
        "UPDATE users SET password_hash = 'x' WHERE id = :id",
        "UPDATE users SET is_default_super_admin = false WHERE id = :id",
        "DELETE FROM users WHERE id = :id",
    ):
        for role in (None, "app_rw"):
            with pytest.raises(DBAPIError, match="PROTECTED_ACCOUNT"):
                await _exec(db_engine, sql, role=role, id=default_sa.id)


async def test_default_super_admin_login_bookkeeping_allowed(db_engine: AsyncEngine, default_sa) -> None:
    await _exec(
        db_engine,
        "UPDATE users SET last_login_at = now(), failed_login_count = 1, locked_until = NULL, "
        "token_version = token_version + 1 WHERE id = :id",
        role="app_rw",
        id=default_sa.id,
    )


async def test_other_users_remain_editable(db_session: AsyncSession) -> None:
    sa = await f.make_user(db_session, "super_admin")
    sa.name = "Renamed daily SA"
    await db_session.commit()


async def test_only_one_default_super_admin(db_session: AsyncSession, default_sa) -> None:
    with pytest.raises(IntegrityError, match="uq_users_one_default_super_admin"):
        await f.make_user(db_session, "super_admin", is_default_super_admin=True)
    await db_session.rollback()


# --------------------------------------------------------------------------- partial unique indexes


async def test_second_active_link_per_role_rejected(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    assessment = await f.make_assessment(db_session, org)
    v1 = await f.make_version(db_session, assessment)
    role = await f.make_role(db_session, org, version=v1)
    v2 = await f.make_version(db_session, assessment, version_no=2)
    await db_session.commit()
    org_id, role_id, v2_id = org.id, role.id, v2.id

    db_session.add(RoleAssessmentLink(org_id=org_id, role_id=role_id, assessment_version_id=v2_id))
    with pytest.raises(IntegrityError, match="uq_role_assessment_links_active_role"):
        await db_session.flush()
    await db_session.rollback()

    # Replacing: unlink the active one first, then a new active link is fine.
    await db_session.execute(
        text("UPDATE role_assessment_links SET unlinked_at = now() WHERE role_id = :r"), {"r": role_id}
    )
    db_session.add(RoleAssessmentLink(org_id=org_id, role_id=role_id, assessment_version_id=v2_id))
    await db_session.commit()


async def test_one_open_help_request_per_invite(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    invite = await f.make_invite(db_session, org)
    org_id, invite_id = org.id, invite.id
    db_session.add(HelpRequest(org_id=org_id, invite_id=invite_id, reason="no_camera"))
    await db_session.commit()
    db_session.add(HelpRequest(org_id=org_id, invite_id=invite_id, reason="extra_time"))
    with pytest.raises(IntegrityError, match="uq_help_requests_open_per_invite"):
        await db_session.flush()
    await db_session.rollback()
    await db_session.execute(
        text("UPDATE help_requests SET status = 'declined' WHERE invite_id = :i"), {"i": invite_id}
    )
    db_session.add(HelpRequest(org_id=org_id, invite_id=invite_id, reason="extra_time"))
    await db_session.commit()


async def test_duplicate_invite_email_needs_override(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    assessment = await f.make_assessment(db_session, org)
    version = await f.make_version(db_session, assessment)
    role = await f.make_role(db_session, org, version=version)
    await f.make_invite(db_session, org, role, version, email="dup@example.com")
    await db_session.commit()
    with pytest.raises(IntegrityError, match="uq_invites_role_email_hash"):
        await f.make_invite(db_session, org, role, version, email=" DUP@example.com")
    await db_session.rollback()
    for obj in (org, role, version):
        await db_session.refresh(obj)
    await f.make_invite(db_session, org, role, version, email="dup@example.com", duplicate_override=True)
    await db_session.commit()


async def test_one_attempt_per_invite(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    invite = await f.make_invite(db_session, org)
    await f.make_attempt(db_session, invite)
    with pytest.raises(IntegrityError, match="uq_attempts_invite_id"):
        await f.make_attempt(db_session, invite)
    await db_session.rollback()


async def test_super_admin_has_no_org_and_others_need_one(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    with pytest.raises(IntegrityError, match="super_admin_has_no_org"):
        await f.make_user(db_session, "super_admin", org_id=org.id)
    await db_session.rollback()


async def test_check_constraint_rejects_unknown_question_type(db_session: AsyncSession) -> None:
    org = await f.make_org(db_session)
    assessment = await f.make_assessment(db_session, org)
    version = await f.make_version(db_session, assessment, status="draft", questions=0)
    db_session.add(Question(version_id=version.id, org_id=org.id, position=1, type="essay", prompt_html="p"))
    with pytest.raises(IntegrityError, match="ck_questions_type"):
        await db_session.flush()
    await db_session.rollback()
