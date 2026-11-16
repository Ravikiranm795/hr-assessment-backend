"""Baseline schema (spec section 5; B2 checklist in spec 14.3).

Hand-reviewed. Besides the tables (generated from ``app/db`` models, then reviewed) this
revision creates, in plain SQL:

* extensions ``citext`` and ``pgcrypto``; ``uuid_generate_v7()`` (DB fallback for the
  app-side v7 ids of ``audit_logs`` and ``proctor_events``);
* database roles ``app_rw`` (application), ``reports_ro`` (read-only reports) and
  ``maintenance`` (audit partition upkeep). Roles are cluster-wide, so creation is
  idempotent and the downgrade leaves them in place. They are NOLOGIN unless
  ``APP_RW_DB_PASSWORD`` / ``REPORTS_RO_DB_PASSWORD`` / ``MAINTENANCE_DB_PASSWORD`` are set
  when the migration runs (then LOGIN with that password);
* RLS (spec 4.1) on every tenant table, ENABLE + FORCE, policy
  ``org_id = app_current_org_id() OR app_is_platform()``; child tables without
  ``org_id`` inherit their parent's visibility. ``app_current_org_id()`` treats an empty
  ``app.org_id`` (what a finished ``SET LOCAL`` leaves behind) as NULL. Superusers and
  BYPASSRLS roles skip RLS, so the application must connect as (or ``SET ROLE``) ``app_rw``;
* ``audit_logs`` monthly range partitions for 14 months from the current UTC month plus a
  DEFAULT partition, and SECURITY DEFINER functions ``audit_ensure_partitions(months,
  start)`` / ``audit_drop_partitions_before(cutoff)`` executable only by ``maintenance``;
* triggers: audit append-only (UPDATE, DELETE, TRUNCATE raise ``AUDIT_APPEND_ONLY``);
  published assessment versions and their questions and options immutable
  (``ASSESSMENT_IMMUTABLE``; deleting a whole version, the 3-version purge, is allowed);
  the default super admin row cannot be deleted or edited except for login bookkeeping
  columns (``PROTECTED_ACCOUNT``). The only bypass is ``SET session_replication_role =
  replica``, which requires a superuser;
* grants: ``app_rw`` full DML except ``audit_logs`` (INSERT, SELECT only); ``reports_ro``
  SELECT on everything except auth secrets tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PASSWORD_RE = re.compile(r"[A-Za-z0-9!#$%&()*+,./:;<=>?@^_~-]{12,128}")
DB_ROLES = ("app_rw", "reports_ro", "maintenance")
ROLE_PASSWORD_ENV = {
    "app_rw": "APP_RW_DB_PASSWORD",
    "reports_ro": "REPORTS_RO_DB_PASSWORD",
    "maintenance": "MAINTENANCE_DB_PASSWORD",
}

# Tables isolated directly by their own org_id column.
ORG_TABLES = (
    "users",
    "misuse_flags",
    "org_feature_flags",
    "branding_themes",
    "assets",
    "documents",
    "org_email_settings",
    "org_ai_settings",
    "ai_usage_log",
    "assessments",
    "assessment_versions",
    "questions",
    "open_roles",
    "role_assessment_links",
    "question_bank_items",
    "invites",
    "help_requests",
    "attempts",
    "proctor_events",
    "decisions",
    "notifications",
    "audit_logs",
    "report_requests",
)
# Child tables without org_id: (table, parent table, fk column). Visible iff the parent row is.
CHILD_TABLES = (
    ("document_versions", "documents", "document_id"),
    ("question_options", "questions", "question_id"),
    ("attempt_answers", "attempts", "attempt_id"),
    ("user_gadgets", "users", "user_id"),
    ("password_history", "users", "user_id"),
    ("refresh_tokens", "users", "user_id"),
)
# Not tenant data: global catalogs. (Auth tables are child tables keyed on user_id; login and refresh
# run in platform mode because they happen before an organization is known.)
NON_TENANT_TABLES = ("feature_flags", "gadget_catalog")
# reports_ro never reads these.
REPORTS_EXCLUDED = ("password_history", "refresh_tokens", "org_email_settings", "org_ai_settings")
# reports_ro reads these tables column by column, never the credential and secret columns. Candidate
# email stays readable (ciphertext only, the key stays in the app) for report-time decryption (7.4.9).
REPORTS_HIDDEN_COLUMNS = {
    "users": ("password_hash", "totp_secret_enc", "totp_recovery_codes_hash", "google_sub"),
    "invites": ("token_hash", "session_binding_hash", "candidate_phone_enc"),
}

AUDIT_INITIAL_MONTHS = 14

PRE_SQL = r"""
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    ts_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000);
    b bytea := gen_random_bytes(16);
BEGIN
    b := overlay(b PLACING substring(int8send(ts_ms) FROM 3) FROM 1 FOR 6);
    b := set_byte(b, 6, (get_byte(b, 6) & 15) | 112);   -- version 7
    b := set_byte(b, 8, (get_byte(b, 8) & 63) | 128);   -- RFC 4122 variant
    RETURN encode(b, 'hex')::uuid;
END $$;

CREATE OR REPLACE FUNCTION app_current_org_id() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app_is_platform() RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT coalesce(current_setting('app.is_platform', true), '') = 'on' $$;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['app_rw', 'reports_ro', 'maintenance'] LOOP
        BEGIN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', r);
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;  -- already created (roles are cluster-wide; another database may own it)
        END;
    END LOOP;
END $$;
"""

TRIGGERS_SQL = r"""
-- Append-only audit log (spec 4.5). Row triggers clone onto every partition.
CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'AUDIT_APPEND_ONLY: audit_logs rows cannot be changed or removed';
END $$;
CREATE TRIGGER audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only();
CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only();

-- Published assessment versions are immutable (S-04, 5.4). DELETE (version purge) is allowed.
CREATE OR REPLACE FUNCTION assessment_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'published' THEN
        RAISE EXCEPTION 'ASSESSMENT_IMMUTABLE: published assessment version % cannot be changed', OLD.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER assessment_versions_published_immutable BEFORE UPDATE ON assessment_versions
    FOR EACH ROW EXECUTE FUNCTION assessment_versions_immutable();

-- Lookups run as owner so the answer does not depend on the caller's RLS context.
CREATE OR REPLACE FUNCTION version_is_published(v uuid) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM assessment_versions WHERE id = v AND status = 'published')
$$;
CREATE OR REPLACE FUNCTION question_in_published_version(q uuid) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM questions qq JOIN assessment_versions v ON v.id = qq.version_id
        WHERE qq.id = q AND v.status = 'published'
    )
$$;

-- When a whole version is deleted, the cascaded deletes of its questions and options see
-- the parent already gone, so the purge passes.
CREATE OR REPLACE FUNCTION questions_immutable_when_published() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND version_is_published(OLD.version_id) THEN
        RAISE EXCEPTION 'ASSESSMENT_IMMUTABLE: question % belongs to a published version', OLD.id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND version_is_published(NEW.version_id) THEN
        RAISE EXCEPTION 'ASSESSMENT_IMMUTABLE: version % is published', NEW.version_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER questions_published_immutable BEFORE INSERT OR UPDATE OR DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION questions_immutable_when_published();

CREATE OR REPLACE FUNCTION question_options_immutable_when_published() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND question_in_published_version(OLD.question_id) THEN
        RAISE EXCEPTION 'ASSESSMENT_IMMUTABLE: option % belongs to a published version', OLD.id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND question_in_published_version(NEW.question_id) THEN
        RAISE EXCEPTION 'ASSESSMENT_IMMUTABLE: question % belongs to a published version', NEW.question_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER question_options_published_immutable BEFORE INSERT OR UPDATE OR DELETE ON question_options
    FOR EACH ROW EXECUTE FUNCTION question_options_immutable_when_published();

-- Protected default (break-glass) super admin (A-05). Only login bookkeeping may change:
-- token_version, failed_login_count, locked_until, last_login_at, updated_at.
CREATE OR REPLACE FUNCTION users_protect_default_super_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_default_super_admin THEN
            RAISE EXCEPTION 'PROTECTED_ACCOUNT: the default super admin cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.is_default_super_admin AND (
        NEW.id, NEW.org_id, NEW.email, NEW.name, NEW.role, NEW.password_hash, NEW.must_change_password,
        NEW.status, NEW.is_default_super_admin, NEW.google_sub, NEW.totp_secret_enc, NEW.totp_enabled,
        NEW.totp_recovery_codes_hash, NEW.locale, NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM (
        OLD.id, OLD.org_id, OLD.email, OLD.name, OLD.role, OLD.password_hash, OLD.must_change_password,
        OLD.status, OLD.is_default_super_admin, OLD.google_sub, OLD.totp_secret_enc, OLD.totp_enabled,
        OLD.totp_recovery_codes_hash, OLD.locale, OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'PROTECTED_ACCOUNT: the default super admin cannot be edited';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER users_default_super_admin_protected BEFORE UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION users_protect_default_super_admin();
"""

PARTITIONS_SQL = r"""
CREATE OR REPLACE FUNCTION audit_ensure_partitions(
    months integer,
    start_month date DEFAULT (date_trunc('month', now() AT TIME ZONE 'UTC'))::date
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    m date := date_trunc('month', start_month)::date;
    created integer := 0;
    part text;
BEGIN
    FOR i IN 1..months LOOP
        part := format('audit_logs_y%sm%s', to_char(m, 'YYYY'), to_char(m, 'MM'));
        IF to_regclass(part) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                part, m::timestamp AT TIME ZONE 'UTC', (m + interval '1 month')::timestamp AT TIME ZONE 'UTC'
            );
            -- Partitions are reached only through audit_logs (where RLS and grants apply).
            EXECUTE format('REVOKE ALL ON %I FROM PUBLIC, app_rw, reports_ro, maintenance', part);
            created := created + 1;
        END IF;
        m := (m + interval '1 month')::date;
    END LOOP;
    RETURN created;
END $$;

CREATE OR REPLACE FUNCTION audit_drop_partitions_before(cutoff date) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    r record;
    part_month date;
    dropped integer := 0;
BEGIN
    FOR r IN
        SELECT c.relname FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'audit_logs' AND c.relname ~ '^audit_logs_y[0-9]{4}m[0-9]{2}$'
    LOOP
        part_month := to_date(substring(r.relname FROM 'y([0-9]{4})m') || substring(r.relname FROM 'm([0-9]{2})$'), 'YYYYMM');
        IF (part_month + interval '1 month')::date <= cutoff THEN
            EXECUTE format('DROP TABLE %I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END $$;

REVOKE ALL ON FUNCTION audit_ensure_partitions(integer, date) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit_drop_partitions_before(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit_ensure_partitions(integer, date) TO maintenance;
GRANT EXECUTE ON FUNCTION audit_drop_partitions_before(date) TO maintenance;

CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT;
REVOKE ALL ON audit_logs_default FROM PUBLIC;
"""

DOWN_FUNCTIONS = (
    "audit_ensure_partitions(integer, date)",
    "audit_drop_partitions_before(date)",
    "users_protect_default_super_admin()",
    "question_options_immutable_when_published()",
    "questions_immutable_when_published()",
    "question_in_published_version(uuid)",
    "version_is_published(uuid)",
    "assessment_versions_immutable()",
    "audit_logs_append_only()",
    "app_is_platform()",
    "app_current_org_id()",
    "uuid_generate_v7()",
)


def _pre_tables() -> None:
    op.execute(PRE_SQL)
    for role, env in ROLE_PASSWORD_ENV.items():
        password = os.environ.get(env)
        if password:
            if not ROLE_PASSWORD_RE.fullmatch(password):
                raise RuntimeError(f"{env}: 12-128 characters from A-Z a-z 0-9 and ! # $ % & ( ) * + , . / : ; < = > ? @ ^ _ ~ -")
            # utility statements take no bind parameters; the charset check above makes the literal safe
            op.execute(
                "DO $$ BEGIN PERFORM set_config('log_statement', 'none', true); "
                "EXCEPTION WHEN insufficient_privilege THEN NULL; END $$"
            )
            op.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{password}'")


def _rls() -> None:
    tenant_check = "org_id = app_current_org_id() OR app_is_platform()"
    statements = []
    for table in ORG_TABLES:
        statements.append(f"CREATE POLICY tenant_isolation ON {table} USING ({tenant_check}) WITH CHECK ({tenant_check})")
    org_check = "id = app_current_org_id() OR app_is_platform()"
    statements.append(f"CREATE POLICY tenant_isolation ON organizations USING ({org_check}) WITH CHECK ({org_check})")
    for table, parent, fk in CHILD_TABLES:
        check = f"app_is_platform() OR EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk})"
        statements.append(f"CREATE POLICY tenant_isolation ON {table} USING ({check}) WITH CHECK ({check})")
    for table in (*ORG_TABLES, "organizations", *(c[0] for c in CHILD_TABLES)):
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for sql in statements:
        op.execute(sql)


def _grants() -> None:
    all_tables = (*ORG_TABLES, "organizations", *(c[0] for c in CHILD_TABLES), *NON_TENANT_TABLES)
    op.execute("GRANT USAGE ON SCHEMA public TO app_rw, reports_ro, maintenance")
    dml = [t for t in all_tables if t != "audit_logs"]
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(dml)} TO app_rw")
    op.execute("GRANT SELECT, INSERT ON audit_logs TO app_rw")
    readable = [t for t in all_tables if t not in REPORTS_EXCLUDED and t not in REPORTS_HIDDEN_COLUMNS]
    op.execute(f"GRANT SELECT ON {', '.join(readable)} TO reports_ro")
    for table, hidden in REPORTS_HIDDEN_COLUMNS.items():
        hidden_sql = ", ".join(f"'{c}'" for c in hidden)
        op.execute(
            f"""DO $$ DECLARE cols text; BEGIN
            SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = '{table}' AND column_name NOT IN ({hidden_sql});
            EXECUTE format('GRANT SELECT (%s) ON {table} TO reports_ro', cols);
            END $$"""
        )


def _post_tables() -> None:
    op.execute(PARTITIONS_SQL)
    op.execute(f"SELECT audit_ensure_partitions({AUDIT_INITIAL_MONTHS})")
    op.execute(TRIGGERS_SQL)
    _rls()
    _grants()


def upgrade() -> None:
    _pre_tables()
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('audit_logs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('actor_type', sa.Text(), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('actor_name_snapshot', sa.Text(), nullable=True),
    sa.Column('actor_role', sa.Text(), nullable=True),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('entity_type', sa.Text(), nullable=True),
    sa.Column('entity_id', sa.Text(), nullable=True),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('result', sa.Text(), server_default='success', nullable=False),
    sa.Column('ip', sa.Text(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("actor_type IN ('user', 'candidate', 'system')", name=op.f('ck_audit_logs_actor_type')),
    sa.PrimaryKeyConstraint('id', 'created_at', name=op.f('pk_audit_logs')),
    postgresql_partition_by='RANGE (created_at)'
    )
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
    op.create_index('ix_audit_logs_entity', 'audit_logs', ['entity_type', 'entity_id'], unique=False)
    op.create_index('ix_audit_logs_org_id_created_at', 'audit_logs', ['org_id', 'created_at'], unique=False)
    op.create_table('feature_flags',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('default_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('visible_to_org', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_feature_flags'))
    )
    op.create_table('gadget_catalog',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('title_key', sa.Text(), nullable=False),
    sa.Column('available_for', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('default_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('default_order', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_gadget_catalog'))
    )
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('slug', postgresql.CITEXT(), nullable=False),
    sa.Column('status', sa.Text(), server_default='active', nullable=False),
    sa.Column('retention_days', sa.Integer(), server_default='90', nullable=False),
    sa.Column('invite_default_expiry_hours', sa.Integer(), server_default='72', nullable=False),
    sa.Column('session_timeout_minutes', sa.Integer(), server_default='30', nullable=False),
    sa.Column('allowed_google_domains', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
    sa.Column('password_policy', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"min_length": 12, "history": 5, "blocklist": true}\'::jsonb'), nullable=False),
    sa.Column('totp_required_for_admins', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('locale', sa.Text(), server_default='en', nullable=False),
    sa.Column('timezone', sa.Text(), server_default='UTC', nullable=False),
    sa.Column('is_demo', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f('ck_organizations_status')),
    sa.CheckConstraint('retention_days BETWEEN 7 AND 730', name=op.f('ck_organizations_retention_days_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('slug', name=op.f('uq_organizations_slug'))
    )
    op.create_table('assets',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('storage_key', sa.Text(), nullable=False),
    sa.Column('content_type', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.Text(), nullable=True),
    sa.Column('encrypted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('logo', 'favicon', 'snapshot', 'baseline', 'doc_pdf')", name=op.f('ck_assets_kind')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_assets_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assets')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_assets_storage_key'))
    )
    op.create_index('ix_assets_org_id', 'assets', ['org_id'], unique=False)
    op.create_table('documents',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('doc_type', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("doc_type IN ('privacy', 'terms', 'consent', 'other')", name=op.f('ck_documents_doc_type')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_documents_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    op.create_index('ix_documents_org_id', 'documents', ['org_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('status', sa.Text(), server_default='active', nullable=False),
    sa.Column('is_default_super_admin', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('google_sub', sa.Text(), nullable=True),
    sa.Column('totp_secret_enc', sa.Text(), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('totp_recovery_codes_hash', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('token_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('failed_login_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locale', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(role = 'super_admin') = (org_id IS NULL)", name=op.f('ck_users_super_admin_has_no_org')),
    sa.CheckConstraint("NOT is_default_super_admin OR role = 'super_admin'", name=op.f('ck_users_default_sa_is_super_admin')),
    sa.CheckConstraint("role IN ('super_admin', 'org_admin', 'hr_user')", name=op.f('ck_users_role')),
    sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f('ck_users_status')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_users_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('google_sub', name=op.f('uq_users_google_sub'))
    )
    op.create_index('ix_users_org_id', 'users', ['org_id'], unique=False)
    op.create_index('uq_users_one_default_super_admin', 'users', ['is_default_super_admin'], unique=True, postgresql_where=sa.text('is_default_super_admin'))
    op.create_table('ai_usage_log',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('feature', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('tokens_in', sa.Integer(), nullable=True),
    sa.Column('tokens_out', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("feature IN ('question_gen', 'scoring', 'report_chat')", name=op.f('ck_ai_usage_log_feature')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_ai_usage_log_org_id_organizations')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ai_usage_log_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_usage_log'))
    )
    op.create_index('ix_ai_usage_log_org_id', 'ai_usage_log', ['org_id', 'created_at'], unique=False)
    op.create_table('assessments',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Text(), server_default='active', nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'archived')", name=op.f('ck_assessments_status')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_assessments_org_id_organizations')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_assessments_owner_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessments'))
    )
    op.create_index('ix_assessments_org_id', 'assessments', ['org_id'], unique=False)
    op.create_table('branding_themes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('primary_color', sa.Text(), nullable=True),
    sa.Column('accent_color', sa.Text(), nullable=True),
    sa.Column('background_style', sa.Text(), nullable=True),
    sa.Column('font_key', sa.Text(), nullable=True),
    sa.Column('logo_asset_id', sa.UUID(), nullable=True),
    sa.Column('favicon_asset_id', sa.UUID(), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(scope = 'org') = (org_id IS NOT NULL)", name=op.f('ck_branding_themes_org_scope_has_org')),
    sa.CheckConstraint("scope IN ('platform', 'org')", name=op.f('ck_branding_themes_scope')),
    sa.ForeignKeyConstraint(['favicon_asset_id'], ['assets.id'], name=op.f('fk_branding_themes_favicon_asset_id_assets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['logo_asset_id'], ['assets.id'], name=op.f('fk_branding_themes_logo_asset_id_assets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_branding_themes_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_branding_themes_updated_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_branding_themes'))
    )
    op.create_index('uq_branding_themes_org', 'branding_themes', ['org_id'], unique=True, postgresql_where=sa.text("scope = 'org'"))
    op.create_index('uq_branding_themes_platform', 'branding_themes', ['scope'], unique=True, postgresql_where=sa.text("scope = 'platform'"))
    op.create_table('document_versions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('content_html', sa.Text(), nullable=True),
    sa.Column('pdf_asset_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.Text(), server_default='draft', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'published')", name=op.f('ck_document_versions_status')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_versions_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['pdf_asset_id'], ['assets.id'], name=op.f('fk_document_versions_pdf_asset_id_assets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['published_by'], ['users.id'], name=op.f('fk_document_versions_published_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_versions'))
    )
    op.create_index('uq_document_versions_document_id_version_no', 'document_versions', ['document_id', 'version_no'], unique=True)
    op.create_table('misuse_flags',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('raised_by', sa.UUID(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='open', nullable=False),
    sa.Column('raised_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cleared_by', sa.UUID(), nullable=True),
    sa.Column('closing_note', sa.Text(), nullable=True),
    sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'cleared')", name=op.f('ck_misuse_flags_status')),
    sa.ForeignKeyConstraint(['cleared_by'], ['users.id'], name=op.f('fk_misuse_flags_cleared_by_users')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_misuse_flags_org_id_organizations')),
    sa.ForeignKeyConstraint(['raised_by'], ['users.id'], name=op.f('fk_misuse_flags_raised_by_users')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_misuse_flags_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_misuse_flags'))
    )
    op.create_index('ix_misuse_flags_org_id', 'misuse_flags', ['org_id'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('entity_type', sa.Text(), nullable=True),
    sa.Column('entity_id', sa.UUID(), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_notifications_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notifications_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications'))
    )
    op.create_index('ix_notifications_org_id', 'notifications', ['org_id'], unique=False)
    op.create_index('ix_notifications_user_id_read_at', 'notifications', ['user_id', 'read_at'], unique=False)
    op.create_table('open_roles',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('department', sa.Text(), nullable=True),
    sa.Column('experience_level', sa.Text(), nullable=True),
    sa.Column('vacancies', sa.Integer(), server_default='1', nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Text(), server_default='draft', nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'open', 'closed')", name=op.f('ck_open_roles_status')),
    sa.CheckConstraint('vacancies >= 1', name=op.f('ck_open_roles_vacancies_positive')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_open_roles_org_id_organizations')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_open_roles_owner_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_open_roles'))
    )
    op.create_index('ix_open_roles_org_id', 'open_roles', ['org_id'], unique=False)
    op.create_index('ix_open_roles_owner_id', 'open_roles', ['owner_id'], unique=False)
    op.create_table('org_ai_settings',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('mode', sa.Text(), server_default='api', nullable=False),
    sa.Column('base_url', sa.Text(), nullable=True),
    sa.Column('api_key_enc', sa.Text(), nullable=True),
    sa.Column('cli_command_key', sa.Text(), nullable=True),
    sa.Column('monthly_call_cap', sa.Integer(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('api', 'cli')", name=op.f('ck_org_ai_settings_mode')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_org_ai_settings_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_org_ai_settings_updated_by_users')),
    sa.PrimaryKeyConstraint('org_id', name=op.f('pk_org_ai_settings'))
    )
    op.create_table('org_email_settings',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('host', sa.Text(), nullable=False),
    sa.Column('port', sa.Integer(), nullable=False),
    sa.Column('security', sa.Text(), server_default='starttls', nullable=False),
    sa.Column('username', sa.Text(), nullable=True),
    sa.Column('password_enc', sa.Text(), nullable=True),
    sa.Column('sender_name', sa.Text(), nullable=True),
    sa.Column('sender_email', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("security IN ('none', 'starttls', 'tls')", name=op.f('ck_org_email_settings_security')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_org_email_settings_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_org_email_settings_updated_by_users')),
    sa.PrimaryKeyConstraint('org_id', name=op.f('pk_org_email_settings'))
    )
    op.create_table('org_feature_flags',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('flag_key', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['flag_key'], ['feature_flags.key'], name=op.f('fk_org_feature_flags_flag_key_feature_flags'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_org_feature_flags_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_org_feature_flags_updated_by_users')),
    sa.PrimaryKeyConstraint('org_id', 'flag_key', name=op.f('pk_org_feature_flags'))
    )
    op.create_table('password_history',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_password_history_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_password_history'))
    )
    op.create_index('ix_password_history_user_id', 'password_history', ['user_id', 'created_at'], unique=False)
    op.create_table('question_bank_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('prompt_html', sa.Text(), nullable=False),
    sa.Column('marks', sa.Numeric(precision=6, scale=2), server_default='1', nullable=False),
    sa.Column('scoring_mode', sa.Text(), server_default='auto', nullable=False),
    sa.Column('answer_key', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('model_answer', sa.Text(), nullable=True),
    sa.Column('code_language', sa.Text(), nullable=True),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("scoring_mode IN ('auto', 'ai', 'manual')", name=op.f('ck_question_bank_items_scoring_mode')),
    sa.CheckConstraint("type IN ('single_choice', 'multi_choice', 'fill_blank', 'code_gap', 'short_answer')", name=op.f('ck_question_bank_items_type')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_question_bank_items_org_id_organizations')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_question_bank_items_owner_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_question_bank_items'))
    )
    op.create_index('ix_question_bank_items_org_id', 'question_bank_items', ['org_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('family_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replaced_by', sa.UUID(), nullable=True),
    sa.Column('ip', sa.Text(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['replaced_by'], ['refresh_tokens.id'], name=op.f('fk_refresh_tokens_replaced_by_refresh_tokens'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    op.create_index('ix_refresh_tokens_family_id', 'refresh_tokens', ['family_id'], unique=False)
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('report_requests',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('user_text', sa.Text(), nullable=True),
    sa.Column('filter_spec', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('columns', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('format', sa.Text(), server_default='csv', nullable=False),
    sa.Column('status', sa.Text(), server_default='interpreted', nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=True),
    sa.Column('asset_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("format IN ('csv', 'pdf')", name=op.f('ck_report_requests_format')),
    sa.CheckConstraint("source IN ('candidates', 'roles', 'assessments', 'team', 'audit')", name=op.f('ck_report_requests_source')),
    sa.CheckConstraint("status IN ('interpreted', 'confirmed', 'generated', 'failed')", name=op.f('ck_report_requests_status')),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_report_requests_asset_id_assets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_report_requests_org_id_organizations')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_report_requests_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_requests'))
    )
    op.create_index('ix_report_requests_org_id', 'report_requests', ['org_id', 'created_at'], unique=False)
    op.create_table('user_gadgets',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('gadget_key', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('size', sa.Text(), server_default='md', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.ForeignKeyConstraint(['gadget_key'], ['gadget_catalog.key'], name=op.f('fk_user_gadgets_gadget_key_gadget_catalog'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_gadgets_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'gadget_key', name=op.f('pk_user_gadgets'))
    )
    op.create_table('assessment_versions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('assessment_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), server_default='draft', nullable=False),
    sa.Column('duration_minutes', sa.Integer(), server_default='60', nullable=False),
    sa.Column('pass_mark_percent', sa.Numeric(precision=5, scale=2), server_default='50', nullable=False),
    sa.Column('instructions_html', sa.Text(), nullable=True),
    sa.Column('header_config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('footer_html', sa.Text(), nullable=True),
    sa.Column('proctoring_config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"snapshot_interval_seconds": 60}\'::jsonb'), nullable=False),
    sa.Column('shuffle_questions', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('shuffle_options', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('total_marks', sa.Numeric(precision=8, scale=2), server_default='0', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'published')", name=op.f('ck_assessment_versions_status')),
    sa.CheckConstraint('duration_minutes > 0', name=op.f('ck_assessment_versions_duration_positive')),
    sa.CheckConstraint('pass_mark_percent BETWEEN 0 AND 100', name=op.f('ck_assessment_versions_pass_mark_range')),
    sa.ForeignKeyConstraint(['assessment_id'], ['assessments.id'], name=op.f('fk_assessment_versions_assessment_id_assessments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_assessment_versions_org_id_organizations')),
    sa.ForeignKeyConstraint(['published_by'], ['users.id'], name=op.f('fk_assessment_versions_published_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_versions'))
    )
    op.create_index('ix_assessment_versions_org_id', 'assessment_versions', ['org_id'], unique=False)
    op.create_index('uq_assessment_versions_assessment_id_version_no', 'assessment_versions', ['assessment_id', 'version_no'], unique=True)
    op.create_index('uq_assessment_versions_one_draft', 'assessment_versions', ['assessment_id'], unique=True, postgresql_where=sa.text("status = 'draft'"))
    op.create_table('invites',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=True),
    sa.Column('candidate_name', sa.Text(), nullable=False),
    sa.Column('candidate_email_enc', sa.Text(), nullable=True),
    sa.Column('candidate_email_hash', sa.Text(), nullable=False),
    sa.Column('candidate_phone_enc', sa.Text(), nullable=True),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='invited', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('camera_exempt', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('time_multiplier', sa.Numeric(precision=4, scale=2), server_default='1.0', nullable=False),
    sa.Column('extra_minutes', sa.Integer(), server_default='0', nullable=False),
    sa.Column('accommodation_reason', sa.Text(), nullable=True),
    sa.Column('accommodation_by', sa.UUID(), nullable=True),
    sa.Column('session_binding_hash', sa.Text(), nullable=True),
    sa.Column('duplicate_override', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('pii_purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('invited', 'started', 'submitted', 'expired', 'revoked')", name=op.f('ck_invites_status')),
    sa.CheckConstraint('extra_minutes >= 0', name=op.f('ck_invites_extra_minutes_non_negative')),
    sa.CheckConstraint('time_multiplier > 0', name=op.f('ck_invites_time_multiplier_positive')),
    sa.ForeignKeyConstraint(['accommodation_by'], ['users.id'], name=op.f('fk_invites_accommodation_by_users')),
    sa.ForeignKeyConstraint(['assessment_version_id'], ['assessment_versions.id'], name=op.f('fk_invites_assessment_version_id_assessment_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_invites_org_id_organizations')),
    sa.ForeignKeyConstraint(['role_id'], ['open_roles.id'], name=op.f('fk_invites_role_id_open_roles')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invites')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_invites_token_hash'))
    )
    op.create_index('ix_invites_org_id', 'invites', ['org_id'], unique=False)
    op.create_index('ix_invites_status_expires_at', 'invites', ['status', 'expires_at'], unique=False)
    op.create_index('uq_invites_role_email_hash', 'invites', ['role_id', 'candidate_email_hash'], unique=True, postgresql_where=sa.text('NOT duplicate_override'))
    op.create_table('questions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('version_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('prompt_html', sa.Text(), nullable=False),
    sa.Column('marks', sa.Numeric(precision=6, scale=2), server_default='1', nullable=False),
    sa.Column('scoring_mode', sa.Text(), server_default='auto', nullable=False),
    sa.Column('answer_key', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('model_answer', sa.Text(), nullable=True),
    sa.Column('code_language', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("scoring_mode IN ('auto', 'ai', 'manual')", name=op.f('ck_questions_scoring_mode')),
    sa.CheckConstraint("type IN ('single_choice', 'multi_choice', 'fill_blank', 'code_gap', 'short_answer')", name=op.f('ck_questions_type')),
    sa.CheckConstraint('marks >= 0', name=op.f('ck_questions_marks_non_negative')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_questions_org_id_organizations')),
    sa.ForeignKeyConstraint(['version_id'], ['assessment_versions.id'], name=op.f('fk_questions_version_id_assessment_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_questions'))
    )
    op.create_index('ix_questions_org_id', 'questions', ['org_id'], unique=False)
    op.create_index('ix_questions_version_id', 'questions', ['version_id', 'position'], unique=False)
    op.create_table('role_assessment_links',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=False),
    sa.Column('linked_by', sa.UUID(), nullable=True),
    sa.Column('linked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('unlinked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('unlinked_by', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['assessment_version_id'], ['assessment_versions.id'], name=op.f('fk_role_assessment_links_assessment_version_id_assessment_versions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['linked_by'], ['users.id'], name=op.f('fk_role_assessment_links_linked_by_users')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_role_assessment_links_org_id_organizations')),
    sa.ForeignKeyConstraint(['role_id'], ['open_roles.id'], name=op.f('fk_role_assessment_links_role_id_open_roles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['unlinked_by'], ['users.id'], name=op.f('fk_role_assessment_links_unlinked_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_role_assessment_links'))
    )
    op.create_index('ix_role_assessment_links_org_id', 'role_assessment_links', ['org_id'], unique=False)
    op.create_index('ix_role_assessment_links_version_id', 'role_assessment_links', ['assessment_version_id'], unique=False)
    op.create_index('uq_role_assessment_links_active_role', 'role_assessment_links', ['role_id'], unique=True, postgresql_where=sa.text('unlinked_at IS NULL'))
    op.create_table('attempts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('invite_id', sa.UUID(), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.Text(), server_default='opened', nullable=False),
    sa.Column('consent_document_version_id', sa.UUID(), nullable=True),
    sa.Column('consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('submit_reason', sa.Text(), nullable=True),
    sa.Column('ip', sa.Text(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('viewport_w', sa.Integer(), nullable=True),
    sa.Column('viewport_h', sa.Integer(), nullable=True),
    sa.Column('baseline_asset_id', sa.UUID(), nullable=True),
    sa.Column('auto_score', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('ai_score', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('final_score', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('score_percent', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=True),
    sa.Column('scoring_status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('decision', sa.Text(), server_default='waiting', nullable=False),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('flag_counts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decision IN ('waiting', 'approved', 'rejected')", name=op.f('ck_attempts_decision')),
    sa.CheckConstraint("scoring_status IN ('pending', 'complete', 'needs_manual')", name=op.f('ck_attempts_scoring_status')),
    sa.CheckConstraint("status IN ('opened', 'consented', 'ready', 'in_progress', 'submitted')", name=op.f('ck_attempts_status')),
    sa.CheckConstraint("submit_reason IS NULL OR submit_reason IN ('candidate', 'timer')", name=op.f('ck_attempts_submit_reason')),
    sa.ForeignKeyConstraint(['assessment_version_id'], ['assessment_versions.id'], name=op.f('fk_attempts_assessment_version_id_assessment_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['baseline_asset_id'], ['assets.id'], name=op.f('fk_attempts_baseline_asset_id_assets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['consent_document_version_id'], ['document_versions.id'], name=op.f('fk_attempts_consent_document_version_id_document_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_attempts_decided_by_users')),
    sa.ForeignKeyConstraint(['invite_id'], ['invites.id'], name=op.f('fk_attempts_invite_id_invites'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_attempts_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attempts')),
    sa.UniqueConstraint('invite_id', name=op.f('uq_attempts_invite_id'))
    )
    op.create_index('ix_attempts_assessment_version_id', 'attempts', ['assessment_version_id'], unique=False)
    op.create_index('ix_attempts_org_id', 'attempts', ['org_id'], unique=False)
    op.create_index('ix_attempts_status_deadline_at', 'attempts', ['status', 'deadline_at'], unique=False)
    op.create_table('question_options',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('question_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('text_html', sa.Text(), nullable=False),
    sa.Column('is_correct', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['question_id'], ['questions.id'], name=op.f('fk_question_options_question_id_questions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_question_options'))
    )
    op.create_index('ix_question_options_question_id', 'question_options', ['question_id', 'position'], unique=False)
    op.create_table('attempt_answers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('question_id', sa.UUID(), nullable=False),
    sa.Column('answer', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('auto_marks', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('ai_marks', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('ai_reason', sa.Text(), nullable=True),
    sa.Column('manual_marks', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('manual_reason', sa.Text(), nullable=True),
    sa.Column('final_marks', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('scored_by', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("scored_by IS NULL OR scored_by IN ('auto', 'ai', 'manual')", name=op.f('ck_attempt_answers_scored_by')),
    sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], name=op.f('fk_attempt_answers_attempt_id_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['question_id'], ['questions.id'], name=op.f('fk_attempt_answers_question_id_questions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attempt_answers'))
    )
    op.create_index('ix_attempt_answers_question_id', 'attempt_answers', ['question_id'], unique=False)
    op.create_index('uq_attempt_answers_attempt_id_question_id', 'attempt_answers', ['attempt_id', 'question_id'], unique=True)
    op.create_table('decisions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('decision', sa.Text(), nullable=False),
    sa.Column('decided_by', sa.UUID(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('is_override', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('previous_decision', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decision IN ('waiting', 'approved', 'rejected')", name=op.f('ck_decisions_decision')),
    sa.CheckConstraint("previous_decision IS NULL OR previous_decision IN ('waiting', 'approved', 'rejected')", name=op.f('ck_decisions_previous_decision')),
    sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], name=op.f('fk_decisions_attempt_id_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_decisions_decided_by_users')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_decisions_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_decisions'))
    )
    op.create_index('ix_decisions_attempt_id', 'decisions', ['attempt_id', 'created_at'], unique=False)
    op.create_index('ix_decisions_org_id', 'decisions', ['org_id'], unique=False)
    op.create_table('help_requests',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('invite_id', sa.UUID(), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='open', nullable=False),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('granted_camera_exempt', sa.Boolean(), nullable=True),
    sa.Column('granted_extra_minutes', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("reason IN ('no_camera', 'camera_not_working', 'extra_time', 'other')", name=op.f('ck_help_requests_reason')),
    sa.CheckConstraint("status IN ('open', 'approved', 'declined')", name=op.f('ck_help_requests_status')),
    sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], name=op.f('fk_help_requests_attempt_id_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_help_requests_decided_by_users')),
    sa.ForeignKeyConstraint(['invite_id'], ['invites.id'], name=op.f('fk_help_requests_invite_id_invites'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_help_requests_org_id_organizations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_help_requests'))
    )
    op.create_index('ix_help_requests_org_id_status', 'help_requests', ['org_id', 'status'], unique=False)
    op.create_index('uq_help_requests_open_per_invite', 'help_requests', ['invite_id'], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_table('proctor_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('snapshot_asset_id', sa.UUID(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('dismissed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('dismissed_by', sa.UUID(), nullable=True),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dismissed_note', sa.Text(), nullable=True),
    sa.CheckConstraint("severity IN ('info', 'minor', 'moderate', 'serious')", name=op.f('ck_proctor_events_severity')),
    sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], name=op.f('fk_proctor_events_attempt_id_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['dismissed_by'], ['users.id'], name=op.f('fk_proctor_events_dismissed_by_users')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_proctor_events_org_id_organizations')),
    sa.ForeignKeyConstraint(['snapshot_asset_id'], ['assets.id'], name=op.f('fk_proctor_events_snapshot_asset_id_assets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proctor_events'))
    )
    op.create_index('ix_proctor_events_attempt_id_received_at', 'proctor_events', ['attempt_id', 'received_at'], unique=False)
    op.create_index('ix_proctor_events_org_id', 'proctor_events', ['org_id'], unique=False)
    # ### end Alembic commands ###
    _post_tables()


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_proctor_events_org_id', table_name='proctor_events')
    op.drop_index('ix_proctor_events_attempt_id_received_at', table_name='proctor_events')
    op.drop_table('proctor_events')
    op.drop_index('uq_help_requests_open_per_invite', table_name='help_requests', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_help_requests_org_id_status', table_name='help_requests')
    op.drop_table('help_requests')
    op.drop_index('ix_decisions_org_id', table_name='decisions')
    op.drop_index('ix_decisions_attempt_id', table_name='decisions')
    op.drop_table('decisions')
    op.drop_index('uq_attempt_answers_attempt_id_question_id', table_name='attempt_answers')
    op.drop_index('ix_attempt_answers_question_id', table_name='attempt_answers')
    op.drop_table('attempt_answers')
    op.drop_index('ix_question_options_question_id', table_name='question_options')
    op.drop_table('question_options')
    op.drop_index('ix_attempts_status_deadline_at', table_name='attempts')
    op.drop_index('ix_attempts_org_id', table_name='attempts')
    op.drop_index('ix_attempts_assessment_version_id', table_name='attempts')
    op.drop_table('attempts')
    op.drop_index('uq_role_assessment_links_active_role', table_name='role_assessment_links', postgresql_where=sa.text('unlinked_at IS NULL'))
    op.drop_index('ix_role_assessment_links_version_id', table_name='role_assessment_links')
    op.drop_index('ix_role_assessment_links_org_id', table_name='role_assessment_links')
    op.drop_table('role_assessment_links')
    op.drop_index('ix_questions_version_id', table_name='questions')
    op.drop_index('ix_questions_org_id', table_name='questions')
    op.drop_table('questions')
    op.drop_index('uq_invites_role_email_hash', table_name='invites', postgresql_where=sa.text('NOT duplicate_override'))
    op.drop_index('ix_invites_status_expires_at', table_name='invites')
    op.drop_index('ix_invites_org_id', table_name='invites')
    op.drop_table('invites')
    op.drop_index('uq_assessment_versions_one_draft', table_name='assessment_versions', postgresql_where=sa.text("status = 'draft'"))
    op.drop_index('uq_assessment_versions_assessment_id_version_no', table_name='assessment_versions')
    op.drop_index('ix_assessment_versions_org_id', table_name='assessment_versions')
    op.drop_table('assessment_versions')
    op.drop_table('user_gadgets')
    op.drop_index('ix_report_requests_org_id', table_name='report_requests')
    op.drop_table('report_requests')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_family_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_question_bank_items_org_id', table_name='question_bank_items')
    op.drop_table('question_bank_items')
    op.drop_index('ix_password_history_user_id', table_name='password_history')
    op.drop_table('password_history')
    op.drop_table('org_feature_flags')
    op.drop_table('org_email_settings')
    op.drop_table('org_ai_settings')
    op.drop_index('ix_open_roles_owner_id', table_name='open_roles')
    op.drop_index('ix_open_roles_org_id', table_name='open_roles')
    op.drop_table('open_roles')
    op.drop_index('ix_notifications_user_id_read_at', table_name='notifications')
    op.drop_index('ix_notifications_org_id', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_misuse_flags_org_id', table_name='misuse_flags')
    op.drop_table('misuse_flags')
    op.drop_index('uq_document_versions_document_id_version_no', table_name='document_versions')
    op.drop_table('document_versions')
    op.drop_index('uq_branding_themes_platform', table_name='branding_themes', postgresql_where=sa.text("scope = 'platform'"))
    op.drop_index('uq_branding_themes_org', table_name='branding_themes', postgresql_where=sa.text("scope = 'org'"))
    op.drop_table('branding_themes')
    op.drop_index('ix_assessments_org_id', table_name='assessments')
    op.drop_table('assessments')
    op.drop_index('ix_ai_usage_log_org_id', table_name='ai_usage_log')
    op.drop_table('ai_usage_log')
    op.drop_index('uq_users_one_default_super_admin', table_name='users', postgresql_where=sa.text('is_default_super_admin'))
    op.drop_index('ix_users_org_id', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_documents_org_id', table_name='documents')
    op.drop_table('documents')
    op.drop_index('ix_assets_org_id', table_name='assets')
    op.drop_table('assets')
    op.drop_table('organizations')
    op.drop_table('gadget_catalog')
    op.drop_table('feature_flags')
    op.drop_index('ix_audit_logs_org_id_created_at', table_name='audit_logs')
    op.drop_index('ix_audit_logs_entity', table_name='audit_logs')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_table('audit_logs')
    # ### end Alembic commands ###
    for fn in DOWN_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute("REVOKE USAGE ON SCHEMA public FROM app_rw, reports_ro, maintenance")
    # Roles are cluster-wide (other databases may use them) and the extensions may be
    # shared, so both are left in place.
