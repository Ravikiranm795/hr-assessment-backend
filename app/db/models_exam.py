"""Spec 5.5: invites, exam, review, notifications, audit, reports and dashboard tables."""

import uuid
from datetime import datetime
from decimal import Decimal

import uuid6
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import enums
from app.db.base import (
    Base,
    TimestampMixin,
    created_at_col,
    one_of,
    one_of_or_null,
    updated_at_col,
    uuid7_pk,
    uuid_pk,
)


class Invite(TimestampMixin, Base):
    __tablename__ = "invites"
    __table_args__ = (
        one_of("status", enums.INVITE_STATUSES),
        CheckConstraint("time_multiplier > 0", name="time_multiplier_positive"),
        CheckConstraint("extra_minutes >= 0", name="extra_minutes_non_negative"),
        # Soft duplicate rule (S-13): one invite per (role, email) unless explicitly overridden.
        Index(
            "uq_invites_role_email_hash",
            "role_id",
            "candidate_email_hash",
            unique=True,
            postgresql_where=text("NOT duplicate_override"),
        ),
        Index("ix_invites_org_id", "org_id"),
        Index("ix_invites_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("open_roles.id"), nullable=False
    )
    # Set at creation (S-11); nulled only if the version is later purged.
    assessment_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="SET NULL")
    )
    candidate_name: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_email_enc: Mapped[str | None] = mapped_column(Text)
    candidate_email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_phone_enc: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="invited")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    camera_exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    time_multiplier: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, server_default="1.0")
    extra_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    accommodation_reason: Mapped[str | None] = mapped_column(Text)
    accommodation_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    session_binding_hash: Mapped[str | None] = mapped_column(Text)
    # True when HR confirmed the duplicate-email warning; exempts the row from the unique index.
    duplicate_override: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pii_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attempt(TimestampMixin, Base):
    __tablename__ = "attempts"
    __table_args__ = (
        one_of("status", enums.ATTEMPT_STATUSES),
        one_of_or_null("submit_reason", enums.SUBMIT_REASONS),
        one_of("scoring_status", enums.SCORING_STATUSES),
        one_of("decision", enums.DECISIONS),
        Index("ix_attempts_org_id", "org_id"),
        Index("ix_attempts_status_deadline_at", "status", "deadline_at"),
        Index("ix_attempts_assessment_version_id", "assessment_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    invite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invites.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Nulled only if the version is purged after the attempt reached a final decision.
    assessment_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="opened")
    consent_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submit_reason: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    viewport_w: Mapped[int | None] = mapped_column(Integer)
    viewport_h: Mapped[int | None] = mapped_column(Integer)
    baseline_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )
    auto_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    ai_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    score_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    scoring_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    decision: Mapped[str] = mapped_column(Text, nullable=False, server_default="waiting")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    flag_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HelpRequest(TimestampMixin, Base):
    __tablename__ = "help_requests"
    __table_args__ = (
        one_of("reason", enums.HELP_REASONS),
        one_of("status", enums.HELP_STATUSES),
        Index(
            "uq_help_requests_open_per_invite",
            "invite_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_help_requests_org_id_status", "org_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    invite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invites.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    granted_camera_exempt: Mapped[bool | None] = mapped_column(Boolean)
    granted_extra_minutes: Mapped[int | None] = mapped_column(Integer)


class AttemptAnswer(Base):
    __tablename__ = "attempt_answers"
    __table_args__ = (
        one_of_or_null("scored_by", enums.SCORED_BY),
        Index("uq_attempt_answers_attempt_id_question_id", "attempt_id", "question_id", unique=True),
        Index("ix_attempt_answers_question_id", "question_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    # Cascades: purging a version removes its answer definitions; the attempt keeps its scores.
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    answer: Mapped[dict | list | str | None] = mapped_column(JSONB)
    auto_marks: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    ai_marks: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    ai_reason: Mapped[str | None] = mapped_column(Text)
    manual_marks: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    manual_reason: Mapped[str | None] = mapped_column(Text)
    final_marks: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    scored_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ProctorEvent(Base):
    __tablename__ = "proctor_events"
    __table_args__ = (
        one_of("severity", enums.SEVERITIES),
        Index("ix_proctor_events_attempt_id_received_at", "attempt_id", "received_at"),
        Index("ix_proctor_events_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid7_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = created_at_col()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    snapshot_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_note: Mapped[str | None] = mapped_column(Text)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        one_of("decision", enums.DECISIONS),
        one_of_or_null("previous_decision", enums.DECISIONS),
        Index("ix_decisions_attempt_id", "attempt_id", "created_at"),
        Index("ix_decisions_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    is_override: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    previous_decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        Index("ix_notifications_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Null only for notifications addressed to a super admin.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    """Spec 4.5. Partitioned by month on ``created_at``; append-only (grants plus trigger).

    ``entity_id`` is text so non-UUID keys (for example a feature flag key) fit.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        one_of("actor_type", enums.ACTOR_TYPES),
        Index("ix_audit_logs_org_id_created_at", "org_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action", "action"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid6.uuid7, server_default=text("uuid_generate_v7()")
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_name_snapshot: Mapped[str | None] = mapped_column(Text)
    actor_role: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text, nullable=False, server_default="success")
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ReportRequest(TimestampMixin, Base):
    __tablename__ = "report_requests"
    __table_args__ = (
        one_of("source", enums.REPORT_SOURCES),
        one_of("format", enums.REPORT_FORMATS),
        one_of("status", enums.REPORT_STATUSES),
        Index("ix_report_requests_org_id", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Null for SA platform-level aggregate reports.
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    user_text: Mapped[str | None] = mapped_column(Text)
    filter_spec: Mapped[dict | None] = mapped_column(JSONB)
    columns: Mapped[list | None] = mapped_column(JSONB)
    format: Mapped[str] = mapped_column(Text, nullable=False, server_default="csv")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="interpreted")
    row_count: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )


class GadgetCatalog(Base):
    __tablename__ = "gadget_catalog"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    title_key: Mapped[str] = mapped_column(Text, nullable=False)
    available_for: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    default_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    default_order: Mapped[int] = mapped_column(Integer, nullable=False)


class UserGadget(Base):
    __tablename__ = "user_gadgets"
    __table_args__ = (PrimaryKeyConstraint("user_id", "gadget_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    gadget_key: Mapped[str] = mapped_column(
        Text, ForeignKey("gadget_catalog.key", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[str] = mapped_column(Text, nullable=False, server_default="md")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
