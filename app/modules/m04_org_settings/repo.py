"""M4 data access. Every query filters ``org_id`` explicitly (RLS is the second layer)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AiUsageLog,
    Asset,
    BrandingTheme,
    Document,
    DocumentVersion,
    OrgAiSettings,
    Organization,
    OrgEmailSettings,
)

# --------------------------------------------------------------------------- organization


async def get_org(session: AsyncSession, org_id: UUID, *, for_update: bool = False) -> Organization | None:
    stmt = select(Organization).where(Organization.id == org_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- email, ai


async def get_email(session: AsyncSession, org_id: UUID) -> OrgEmailSettings | None:
    stmt = select(OrgEmailSettings).where(OrgEmailSettings.org_id == org_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_ai(session: AsyncSession, org_id: UUID) -> OrgAiSettings | None:
    stmt = select(OrgAiSettings).where(OrgAiSettings.org_id == org_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def ai_usage(session: AsyncSession, org_id: UUID, since: datetime, until: datetime) -> list[Any]:
    stmt = (
        select(
            AiUsageLog.feature,
            func.count().label("calls"),
            func.coalesce(func.sum(AiUsageLog.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(AiUsageLog.tokens_out), 0).label("tokens_out"),
        )
        .where(AiUsageLog.org_id == org_id, AiUsageLog.created_at >= since, AiUsageLog.created_at < until)
        .group_by(AiUsageLog.feature)
    )
    return list((await session.execute(stmt)).all())


# --------------------------------------------------------------------------- branding and assets


async def get_theme(session: AsyncSession, org_id: UUID, *, for_update: bool = False) -> BrandingTheme | None:
    stmt = select(BrandingTheme).where(BrandingTheme.org_id == org_id, BrandingTheme.scope == "org")
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_asset(
    session: AsyncSession, org_id: UUID, asset_id: UUID, kind: str | None = None
) -> Asset | None:
    stmt = select(Asset).where(Asset.id == asset_id, Asset.org_id == org_id)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    return (await session.execute(stmt)).scalar_one_or_none()


async def add(session: AsyncSession, obj: Any) -> Any:
    session.add(obj)
    await session.flush()
    return obj


async def delete(session: AsyncSession, obj: Any) -> None:
    await session.delete(obj)
    await session.flush()


# --------------------------------------------------------------------------- documents


async def get_document(
    session: AsyncSession, org_id: UUID, doc_id: UUID, *, for_update: bool = False
) -> Document | None:
    stmt = select(Document).where(Document.id == doc_id, Document.org_id == org_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_documents(
    session: AsyncSession, org_id: UUID, *, doc_type: str | None, order: list[Any], offset: int, limit: int
) -> tuple[list[Document], int]:
    where = [Document.org_id == org_id]
    if doc_type:
        where.append(Document.doc_type == doc_type)
    total = (await session.execute(select(func.count()).select_from(Document).where(*where))).scalar_one()
    stmt = select(Document).where(*where).order_by(*order, Document.id).offset(offset).limit(limit)
    return list((await session.execute(stmt)).scalars().all()), total


async def version_stats(session: AsyncSession, doc_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Per document: version count and the latest published version (by publication time)."""
    if not doc_ids:
        return {}
    stats: dict[UUID, dict[str, Any]] = {d: {"count": 0, "current": None} for d in doc_ids}
    rows = (
        await session.execute(
            select(DocumentVersion.document_id, func.count())
            .where(DocumentVersion.document_id.in_(doc_ids))
            .group_by(DocumentVersion.document_id)
        )
    ).all()
    for doc_id, count in rows:
        stats[doc_id]["count"] = count
    published = (
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id.in_(doc_ids), DocumentVersion.status == "published")
                .order_by(DocumentVersion.published_at.asc(), DocumentVersion.version_no.asc())
            )
        )
        .scalars()
        .all()
    )
    for version in published:
        stats[version.document_id]["current"] = version  # ascending order: the last one wins
    return stats


async def list_versions(session: AsyncSession, doc_id: UUID) -> list[DocumentVersion]:
    stmt = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id)
        .order_by(DocumentVersion.version_no.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_version(
    session: AsyncSession, doc_id: UUID, version_id: UUID, *, for_update: bool = False
) -> DocumentVersion | None:
    stmt = select(DocumentVersion).where(
        DocumentVersion.id == version_id, DocumentVersion.document_id == doc_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def next_version_no(session: AsyncSession, doc_id: UUID) -> int:
    top = (
        await session.execute(
            select(func.max(DocumentVersion.version_no)).where(DocumentVersion.document_id == doc_id)
        )
    ).scalar_one()
    return (top or 0) + 1


async def latest_published(session: AsyncSession, org_id: UUID, doc_type: str) -> DocumentVersion | None:
    stmt = (
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.org_id == org_id, Document.doc_type == doc_type, DocumentVersion.status == "published"
        )
        .order_by(DocumentVersion.published_at.desc(), DocumentVersion.version_no.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_published(session: AsyncSession, doc_id: UUID) -> bool:
    stmt = (
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id, DocumentVersion.status == "published")
    )
    return (await session.execute(stmt)).scalar_one() > 0
