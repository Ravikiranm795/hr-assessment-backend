"""Baseline schema (spec section 5, B2 checklist in 14.3): tables, indexes, catalogs, roles."""

from datetime import UTC, datetime

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import Base

EXPECTED_TABLES = {
    # 5.3 platform, accounts, security
    "organizations",
    "users",
    "password_history",
    "refresh_tokens",
    "misuse_flags",
    "feature_flags",
    "org_feature_flags",
    "branding_themes",
    "assets",
    "documents",
    "document_versions",
    "org_email_settings",
    "org_ai_settings",
    "ai_usage_log",
    # 5.4 assessments and roles
    "assessments",
    "assessment_versions",
    "questions",
    "question_options",
    "open_roles",
    "role_assessment_links",
    # 5.5 invites, exam, review
    "invites",
    "help_requests",
    "attempts",
    "attempt_answers",
    "proctor_events",
    "decisions",
    "notifications",
    "audit_logs",
    "report_requests",
    "gadget_catalog",
    "user_gadgets",
    "question_bank_items",
}

# Appendix E, grouped rows expanded into individual keys.
EXPECTED_FLAGS = {
    "dashboard",
    "dashboard_gadgets",
    "assessments",
    "ai_question_builder",
    "ai_scoring",
    "question_bank",
    "roles",
    "invites",
    "csv_bulk_invite",
    "email_sending",
    "help_requests",
    "camera_proctoring",
    "review",
    "reports_chat",
    "audit_view",
    "audit_export",
    "team",
    "settings",
    "org_branding",
    "policy_documents",
    "ai_provider_setup",
    "watermark",
}

HR, OA, SA = "hr_user", "org_admin", "super_admin"
# Appendix D, in catalog order.
EXPECTED_GADGETS = [
    ("summary_counts", {HR, OA, SA}),
    ("needs_attention", {HR, OA}),
    ("open_roles", {HR, OA}),
    ("recent_activity", {HR, OA}),
    ("candidates_per_week", {HR, OA, SA}),
    ("decision_split", {HR, OA, SA}),
    ("quick_actions", {HR, OA}),
    ("expiring_invites", {HR, OA}),
    ("avg_score_by_role", {HR, OA}),
    ("avg_completion_time", {HR, OA}),
    ("my_assessments", {HR}),
    ("proctoring_flags", {HR, OA}),
    ("roles_by_recruiter", {OA}),
    ("security_events", {OA}),
    ("active_flag_notice", {OA}),
    ("orgs_overview", {SA}),
    ("active_misuse_flags", {SA}),
    ("platform_usage", {SA}),
]


async def _scalars(engine: AsyncEngine, sql: str, **params) -> list:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).scalars())


async def test_all_tables_exist(db_engine: AsyncEngine) -> None:
    tables = await _scalars(
        db_engine,
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
    )
    assert EXPECTED_TABLES <= set(tables), EXPECTED_TABLES - set(tables)
    assert set(Base.metadata.tables) == EXPECTED_TABLES


async def test_extensions_installed(db_engine: AsyncEngine) -> None:
    assert {"citext", "pgcrypto"} <= set(await _scalars(db_engine, "SELECT extname FROM pg_extension"))


async def test_audit_logs_columns_match_spec_4_5(db_engine: AsyncEngine) -> None:
    columns = await _scalars(
        db_engine,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_logs' "
        "ORDER BY ordinal_position",
    )
    assert columns == [
        "id",
        "org_id",
        "actor_type",
        "actor_user_id",
        "actor_name_snapshot",
        "actor_role",
        "action",
        "entity_type",
        "entity_id",
        "before",
        "after",
        "reason",
        "result",
        "ip",
        "user_agent",
        "request_id",
        "created_at",
    ]


async def test_audit_partitions_cover_14_months_from_current_month(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT relkind::text FROM pg_class WHERE relname = 'audit_logs'"))
        ).scalar() == "p"
        bounds = (
            await conn.execute(
                text(
                    "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'audit_logs' ORDER BY c.relname"
                )
            )
        ).all()
    names = {name for name, _ in bounds}
    now = datetime.now(UTC)
    expected = set()
    year, month = now.year, now.month
    for _ in range(14):
        expected.add(f"audit_logs_y{year:04d}m{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    assert expected <= names, expected - names
    assert "audit_logs_default" in names
    assert len(names) == 15


async def test_feature_flag_catalog_seeded(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT key, default_enabled, visible_to_org, description FROM feature_flags")
            )
        ).all()
    assert {r.key for r in rows} == EXPECTED_FLAGS
    assert len(rows) == 22
    assert all(r.default_enabled for r in rows)
    assert all(r.description for r in rows)
    hidden = {r.key for r in rows if not r.visible_to_org}
    assert hidden == {"watermark"}


async def test_gadget_catalog_seeded(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT key, title_key, available_for, default_enabled, default_order "
                    "FROM gadget_catalog ORDER BY default_order"
                )
            )
        ).all()
    assert [(r.key, set(r.available_for)) for r in rows] == EXPECTED_GADGETS
    assert [r.default_order for r in rows] == list(range(1, 19))
    assert all(r.title_key == f"gadgets.{r.key}.title" for r in rows)
    assert all(r.default_enabled for r in rows)


async def test_database_roles_exist(db_engine: AsyncEngine) -> None:
    roles = await _scalars(
        db_engine, "SELECT rolname FROM pg_roles WHERE rolname IN ('app_rw', 'reports_ro', 'maintenance')"
    )
    assert set(roles) == {"app_rw", "reports_ro", "maintenance"}
    supers = await _scalars(
        db_engine,
        "SELECT rolname FROM pg_roles WHERE rolname IN ('app_rw', 'reports_ro', 'maintenance') "
        "AND (rolsuper OR rolbypassrls)",
    )
    assert supers == []


async def test_app_rw_has_only_insert_and_select_on_audit_logs(db_engine: AsyncEngine) -> None:
    privileges = await _scalars(
        db_engine,
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'app_rw' AND table_name = 'audit_logs'",
    )
    assert sorted(privileges) == ["INSERT", "SELECT"]
    partition_grants = await _scalars(
        db_engine,
        "SELECT DISTINCT table_name FROM information_schema.role_table_grants "
        "WHERE grantee IN ('app_rw', 'reports_ro') AND table_name LIKE 'audit_logs\\_%'",
    )
    assert partition_grants == []


async def test_reports_ro_is_read_only(db_engine: AsyncEngine) -> None:
    privileges = await _scalars(
        db_engine,
        "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'reports_ro'",
    )
    assert privileges == ["SELECT"]


@pytest.mark.parametrize(
    ("table", "column", "allowed"),
    [
        ("users", "email", True),
        ("users", "password_hash", False),
        ("users", "totp_secret_enc", False),
        ("users", "totp_recovery_codes_hash", False),
        ("users", "google_sub", False),
        ("invites", "candidate_email_enc", True),
        ("invites", "token_hash", False),
        ("invites", "session_binding_hash", False),
        ("invites", "candidate_phone_enc", False),
    ],
)
async def test_reports_ro_column_grants_hide_credentials(
    db_engine: AsyncEngine, table: str, column: str, allowed: bool
) -> None:
    got = await _scalars(
        db_engine, f"SELECT has_column_privilege('reports_ro', '{table}', '{column}', 'SELECT')"
    )
    assert got == [allowed]


@pytest.mark.parametrize(
    ("table", "index_name"),
    [
        ("role_assessment_links", "uq_role_assessment_links_active_role"),
        ("help_requests", "uq_help_requests_open_per_invite"),
        ("assessment_versions", "uq_assessment_versions_one_draft"),
        ("invites", "uq_invites_role_email_hash"),
        ("users", "uq_users_one_default_super_admin"),
    ],
)
async def test_partial_unique_indexes(db_engine: AsyncEngine, table: str, index_name: str) -> None:
    rows = await _scalars(
        db_engine,
        "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i",
        t=table,
        i=index_name,
    )
    assert len(rows) == 1
    assert "UNIQUE" in rows[0] and "WHERE" in rows[0]


def test_models_match_migrations(migrated_db: str) -> None:
    """The ORM models and the migrated schema describe the same tables and columns."""
    sync_url = make_url(migrated_db).set(drivername="postgresql+psycopg")
    engine = create_engine(sync_url)

    def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
        if type_ == "table" and (name.startswith("audit_logs_") or name == "alembic_version"):
            return False
        return True

    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"include_object": include_object, "compare_type": True}
            )
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_migrations_downgrade_to_base_and_upgrade_again(migrated_db: str) -> None:
    """Round trip on a separate scratch database so the shared session database stays intact."""
    from alembic import command

    from tests.conftest import _BASE_TEST_URL, _libpq_dsn, alembic_config

    scratch = f"{make_url(migrated_db).database}_rt"
    admin = _libpq_dsn(_BASE_TEST_URL)
    url = make_url(migrated_db).set(database=scratch).render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{scratch}"')
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        with psycopg.connect(_libpq_dsn(url)) as conn:
            left = conn.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relname <> 'alembic_version'"
            ).fetchone()[0]
        assert left == 0
        command.upgrade(cfg, "head")
        with psycopg.connect(_libpq_dsn(url)) as conn:
            flags = conn.execute("SELECT count(*) FROM feature_flags").fetchone()[0]
        assert flags == 22
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


async def test_factories_build_a_full_chain_and_valid_token(db_session) -> None:
    from tests import factories as f

    org = await f.make_org(db_session)
    invite = await f.make_invite(db_session, org)
    attempt = await f.make_attempt(db_session, invite, status="submitted")
    headers = await f.auth_headers(db_session, "org_admin", org)
    await db_session.commit()
    assert attempt.org_id == org.id and attempt.assessment_version_id == invite.assessment_version_id
    assert invite.status == "submitted" and invite.raw_token
    assert headers["Authorization"].startswith("Bearer ")

    from app.core import security

    claims = security.decode_token(headers["Authorization"].removeprefix("Bearer "))
    assert claims.role == "org_admin" and claims.org_id == str(org.id)
