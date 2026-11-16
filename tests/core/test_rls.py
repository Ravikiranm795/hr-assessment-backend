"""Row-level security (spec 4.1) checked as the application role.

The fixtures connect as the server superuser, which bypasses RLS. Each check therefore
runs inside a transaction that switches to ``app_rw`` with ``SET LOCAL ROLE`` and sets
the tenant settings exactly as ``core.db.tenant_session`` does.
"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from tests import factories as f

# Tables outside tenant isolation: global catalogs, and user-scoped auth tables that are
# only reachable by user id or token hash (login and refresh run before an org is known).
NON_TENANT_TABLES = {"feature_flags", "gadget_catalog", "password_history", "refresh_tokens"}


@asynccontextmanager
async def as_app(engine: AsyncEngine, org_id=None, platform: bool = False):
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET LOCAL ROLE app_rw"))
            if org_id is not None:
                await conn.execute(text("SELECT set_config('app.org_id', :v, true)"), {"v": str(org_id)})
            if platform:
                await conn.execute(text("SELECT set_config('app.is_platform', 'on', true)"))
            yield conn


async def _ids(conn: AsyncConnection, table: str) -> set:
    return set((await conn.execute(text(f"SELECT id FROM {table}"))).scalars())


@pytest.fixture
async def two_orgs(db_session: AsyncSession):
    """Org A and org B, each with a user, assessment, version, question, role, invite, attempt."""
    data = {}
    for name in ("a", "b"):
        org = await f.make_org(db_session)
        owner = await f.make_user(db_session, "hr_user", org)
        assessment = await f.make_assessment(db_session, org, owner)
        version = await f.make_version(db_session, assessment)
        role = await f.make_role(db_session, org, owner, version=version)
        invite = await f.make_invite(db_session, org, role, version)
        attempt = await f.make_attempt(db_session, invite)
        data[name] = dict(
            org=org,
            user=owner,
            assessment=assessment,
            version=version,
            role=role,
            invite=invite,
            attempt=attempt,
        )
    await db_session.commit()
    return data


async def test_every_tenant_table_has_forced_rls_and_policy(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
                    "AND c.relname <> 'alembic_version'"
                )
            )
        ).all()
    tenant = [r for r in rows if r.relname not in NON_TENANT_TABLES]
    assert len(tenant) == len(rows) - len(NON_TENANT_TABLES)
    missing = [r.relname for r in tenant if not (r.relrowsecurity and r.relforcerowsecurity and r.policies)]
    assert missing == []


async def test_rls_blocks_cross_org_read(db_engine: AsyncEngine, two_orgs) -> None:
    a, b = two_orgs["a"], two_orgs["b"]
    async with as_app(db_engine, org_id=a["org"].id) as conn:
        assert await _ids(conn, "organizations") == {a["org"].id}
        assert await _ids(conn, "users") == {a["user"].id}
        for table, key in [
            ("assessments", "assessment"),
            ("assessment_versions", "version"),
            ("open_roles", "role"),
            ("invites", "invite"),
            ("attempts", "attempt"),
        ]:
            assert await _ids(conn, table) == {a[key].id}, table
        # Child tables without org_id follow their parent's visibility.
        visible_options = await _option_ids(conn)
    async with db_engine.connect() as su:  # superuser view: both orgs' options exist
        all_options = await _option_ids(su)
        b_options = await _option_ids(su, b["version"].id)
    assert b_options and b_options <= all_options
    assert visible_options and not (visible_options & b_options)


async def _option_ids(conn: AsyncConnection, version_id=None) -> set:
    sql = "SELECT o.id FROM question_options o"
    if version_id is None:
        return set((await conn.execute(text(sql))).scalars())
    sql += " WHERE o.question_id IN (SELECT id FROM questions WHERE version_id = :v)"
    return set((await conn.execute(text(sql), {"v": version_id})).scalars())


async def test_rls_without_tenant_setting_sees_nothing(db_engine: AsyncEngine, two_orgs) -> None:
    async with as_app(db_engine) as conn:
        for table in ("organizations", "users", "assessments", "invites", "attempts", "question_options"):
            assert await _ids(conn, table) == set(), table


async def test_rls_platform_mode_sees_all(db_engine: AsyncEngine, two_orgs) -> None:
    a, b = two_orgs["a"], two_orgs["b"]
    async with as_app(db_engine, platform=True) as conn:
        assert {a["org"].id, b["org"].id} <= await _ids(conn, "organizations")
        assert {a["invite"].id, b["invite"].id} <= await _ids(conn, "invites")
        assert {a["attempt"].id, b["attempt"].id} <= await _ids(conn, "attempts")


async def test_rls_blocks_cross_org_write(db_engine: AsyncEngine, two_orgs) -> None:
    a, b = two_orgs["a"], two_orgs["b"]
    async with as_app(db_engine, org_id=a["org"].id) as conn:
        result = await conn.execute(
            text("UPDATE open_roles SET title = 'hijacked' WHERE id = :id"), {"id": b["role"].id}
        )
        assert result.rowcount == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        async with as_app(db_engine, org_id=a["org"].id) as conn:
            await conn.execute(
                text("INSERT INTO assessments (org_id, title, owner_id) VALUES (:org, 'x', :owner)"),
                {"org": b["org"].id, "owner": b["user"].id},
            )


async def test_rls_applies_to_audit_logs(db_engine: AsyncEngine, two_orgs) -> None:
    a, b = two_orgs["a"], two_orgs["b"]
    async with as_app(db_engine, platform=True) as conn:
        for org in (a["org"], b["org"]):
            await conn.execute(
                text("INSERT INTO audit_logs (org_id, actor_type, action) VALUES (:org, 'system', 'TEST')"),
                {"org": org.id},
            )
    async with as_app(db_engine, org_id=a["org"].id) as conn:
        orgs = set((await conn.execute(text("SELECT org_id FROM audit_logs"))).scalars())
    assert orgs == {a["org"].id}


async def test_rls_survives_reset_setting(db_engine: AsyncEngine, two_orgs) -> None:
    """After a SET LOCAL ends, the setting reads as '' on that connection; that must not error."""
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.org_id', :v, true)"), {"v": str(two_orgs["a"]["org"].id)}
            )
        async with conn.begin():
            await conn.execute(text("SET LOCAL ROLE app_rw"))
            assert (await conn.execute(text("SELECT count(*) FROM invites"))).scalar() == 0
