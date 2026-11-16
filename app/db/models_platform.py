"""Spec 5.3: platform, accounts and security tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import enums
from app.db.base import Base, TimestampMixin, created_at_col, one_of, updated_at_col, uuid_pk

DEFAULT_PASSWORD_POLICY = '{"min_length": 12, "history": 5, "blocklist": true}'


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        one_of("status", enums.ORG_STATUSES),
        CheckConstraint("retention_days BETWEEN 7 AND 730", name="retention_days_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    invite_default_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="72")
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    allowed_google_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    password_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text(f"'{DEFAULT_PASSWORD_POLICY}'::jsonb")
    )
    totp_required_for_admins: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        one_of("role", enums.USER_ROLES),
        one_of("status", enums.ACTIVE_STATUSES),
        CheckConstraint("(role = 'super_admin') = (org_id IS NULL)", name="super_admin_has_no_org"),
        CheckConstraint(
            "NOT is_default_super_admin OR role = 'super_admin'", name="default_sa_is_super_admin"
        ),
        Index(
            "uq_users_one_default_super_admin",
            "is_default_super_admin",
            unique=True,
            postgresql_where=text("is_default_super_admin"),
        ),
        Index("ix_users_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    is_default_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    google_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Hashes of the unused TOTP recovery codes (spec 4.2); never the codes themselves.
    totp_recovery_codes_hash: Mapped[list[str] | None] = mapped_column(JSONB)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locale: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class PasswordHistory(Base):
    __tablename__ = "password_history"
    __table_args__ = (Index("ix_password_history_user_id", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class MisuseFlag(TimestampMixin, Base):
    __tablename__ = "misuse_flags"
    __table_args__ = (one_of("status", enums.MISUSE_FLAG_STATUSES), Index("ix_misuse_flags_org_id", "org_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    raised_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    raised_at: Mapped[datetime] = created_at_col()
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    closing_note: Mapped[str | None] = mapped_column(Text)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    default_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    visible_to_org: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    updated_at: Mapped[datetime] = updated_at_col()


class OrgFeatureFlag(Base):
    __tablename__ = "org_feature_flags"
    __table_args__ = (PrimaryKeyConstraint("org_id", "flag_key"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    flag_key: Mapped[str] = mapped_column(
        Text, ForeignKey("feature_flags.key", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = updated_at_col()


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (one_of("kind", enums.ASSET_KINDS), Index("ix_assets_org_id", "org_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    # Null for platform-level assets (SA branding logo and favicon).
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class BrandingTheme(TimestampMixin, Base):
    __tablename__ = "branding_themes"
    __table_args__ = (
        one_of("scope", enums.BRANDING_SCOPES),
        CheckConstraint("(scope = 'org') = (org_id IS NOT NULL)", name="org_scope_has_org"),
        Index("uq_branding_themes_org", "org_id", unique=True, postgresql_where=text("scope = 'org'")),
        Index(
            "uq_branding_themes_platform", "scope", unique=True, postgresql_where=text("scope = 'platform'")
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    primary_color: Mapped[str | None] = mapped_column(Text)
    accent_color: Mapped[str | None] = mapped_column(Text)
    background_style: Mapped[str | None] = mapped_column(Text)
    font_key: Mapped[str | None] = mapped_column(Text)
    logo_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )
    favicon_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (one_of("doc_type", enums.DOC_TYPES), Index("ix_documents_org_id", "org_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class DocumentVersion(TimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        one_of("status", enums.DRAFT_PUBLISHED),
        Index("uq_document_versions_document_id_version_no", "document_id", "version_no", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_html: Mapped[str | None] = mapped_column(Text)
    pdf_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class OrgEmailSettings(TimestampMixin, Base):
    __tablename__ = "org_email_settings"
    __table_args__ = (one_of("security", enums.SMTP_SECURITY),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    security: Mapped[str] = mapped_column(Text, nullable=False, server_default="starttls")
    username: Mapped[str | None] = mapped_column(Text)
    password_enc: Mapped[str | None] = mapped_column(Text)
    sender_name: Mapped[str | None] = mapped_column(Text)
    sender_email: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class OrgAiSettings(TimestampMixin, Base):
    __tablename__ = "org_ai_settings"
    __table_args__ = (one_of("mode", enums.AI_MODES),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="api")
    base_url: Mapped[str | None] = mapped_column(Text)
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    cli_command_key: Mapped[str | None] = mapped_column(Text)
    monthly_call_cap: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class AiUsageLog(Base):
    __tablename__ = "ai_usage_log"
    __table_args__ = (
        one_of("feature", enums.AI_FEATURES),
        Index("ix_ai_usage_log_org_id", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Null for SA calls made outside any organization.
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    feature: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()
