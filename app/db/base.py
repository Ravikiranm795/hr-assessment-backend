"""Declarative base, naming convention and column helpers shared by every model.

The schema itself is owned by ``migrations/versions`` (hand-reviewed baseline). These
models mirror it exactly; ``tests/core/test_schema.py::test_models_match_migrations``
fails if the two drift apart.
"""

import uuid
from datetime import datetime

import uuid6
from sqlalchemy import CheckConstraint, DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Fetch server-generated values (defaults, updated_at) with RETURNING so async code
    # never triggers an implicit lazy load after a flush.
    __mapper_args__ = {"eager_defaults": True}


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID v4 primary key, generated app-side with a database fallback."""
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def uuid7_pk() -> Mapped[uuid.UUID]:
    """UUID v7 primary key for high-volume tables.

    Generated app-side; ``uuid_generate_v7()`` (created by the baseline migration) is the
    fallback for raw SQL inserts that omit the id.
    """
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid6.uuid7, server_default=text("uuid_generate_v7()")
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


def one_of(column: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    """``CHECK (column IN (...))``; text columns with a check instead of PG enum types."""
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


def one_of_or_null(column: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IS NULL OR {column} IN ({allowed})", name=name or column)


class TimestampMixin:
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
