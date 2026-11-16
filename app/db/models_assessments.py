"""Spec 5.4: assessments, versions, questions, open roles and role links.

Published ``assessment_versions`` rows, and the questions and options under them, are
immutable at the database level (trigger, error message ``ASSESSMENT_IMMUTABLE``).
Deleting a published version (the 3-version purge) is allowed and cascades.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import enums
from app.db.base import Base, TimestampMixin, created_at_col, one_of, uuid_pk

DEFAULT_PROCTORING_CONFIG = '{"snapshot_interval_seconds": 60}'


class Assessment(TimestampMixin, Base):
    __tablename__ = "assessments"
    __table_args__ = (one_of("status", enums.ASSESSMENT_STATUSES), Index("ix_assessments_org_id", "org_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AssessmentVersion(TimestampMixin, Base):
    __tablename__ = "assessment_versions"
    __table_args__ = (
        one_of("status", enums.DRAFT_PUBLISHED),
        CheckConstraint("pass_mark_percent BETWEEN 0 AND 100", name="pass_mark_range"),
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
        Index("uq_assessment_versions_assessment_id_version_no", "assessment_id", "version_no", unique=True),
        Index(
            "uq_assessment_versions_one_draft",
            "assessment_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index("ix_assessment_versions_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    pass_mark_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default="50")
    instructions_html: Mapped[str | None] = mapped_column(Text)
    header_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    footer_html: Mapped[str | None] = mapped_column(Text)
    proctoring_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text(f"'{DEFAULT_PROCTORING_CONFIG}'::jsonb")
    )
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    shuffle_options: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    total_marks: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, server_default="0")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Question(TimestampMixin, Base):
    __tablename__ = "questions"
    __table_args__ = (
        one_of("type", enums.QUESTION_TYPES),
        one_of("scoring_mode", enums.SCORING_MODES),
        CheckConstraint("marks >= 0", name="marks_non_negative"),
        Index("ix_questions_version_id", "version_id", "position"),
        Index("ix_questions_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_html: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default="1")
    scoring_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="auto")
    answer_key: Mapped[dict | list | None] = mapped_column(JSONB)
    model_answer: Mapped[str | None] = mapped_column(Text)
    code_language: Mapped[str | None] = mapped_column(Text)


class QuestionOption(TimestampMixin, Base):
    __tablename__ = "question_options"
    __table_args__ = (Index("ix_question_options_question_id", "question_id", "position"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text_html: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class OpenRole(TimestampMixin, Base):
    __tablename__ = "open_roles"
    __table_args__ = (
        one_of("status", enums.ROLE_STATUSES),
        CheckConstraint("vacancies >= 1", name="vacancies_positive"),
        Index("ix_open_roles_org_id", "org_id"),
        Index("ix_open_roles_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    experience_level: Mapped[str | None] = mapped_column(Text)
    vacancies: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class RoleAssessmentLink(Base):
    __tablename__ = "role_assessment_links"
    __table_args__ = (
        Index(
            "uq_role_assessment_links_active_role",
            "role_id",
            unique=True,
            postgresql_where=text("unlinked_at IS NULL"),
        ),
        Index("ix_role_assessment_links_version_id", "assessment_version_id"),
        Index("ix_role_assessment_links_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("open_roles.id", ondelete="CASCADE"), nullable=False
    )
    assessment_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="CASCADE"), nullable=False
    )
    linked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    linked_at: Mapped[datetime] = created_at_col()
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unlinked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class QuestionBankItem(TimestampMixin, Base):
    """Stretch module M5b. Mirrors the question shape so an item converts to a question body."""

    __tablename__ = "question_bank_items"
    __table_args__ = (
        one_of("type", enums.QUESTION_TYPES),
        one_of("scoring_mode", enums.SCORING_MODES),
        Index("ix_question_bank_items_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_html: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, server_default="1")
    scoring_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="auto")
    answer_key: Mapped[dict | list | None] = mapped_column(JSONB)
    model_answer: Mapped[str | None] = mapped_column(Text)
    code_language: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list | None] = mapped_column(JSONB)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
