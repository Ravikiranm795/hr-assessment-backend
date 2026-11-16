"""Audit search, detail and security events (spec 6/M10). Read-only: nothing here writes except
the export audit entry in ``export.py``.

Scope rules: an org admin is always pinned to ``ctx.org_id`` (RLS is the second layer); a super
admin (``ctx.is_platform``) sees every organization and may narrow with ``filters.org_id``.
Stored values are redacted at write time; ``redact`` is applied again on read, so a redacted
field's value can never leave through this module.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import TenantContext
from app.core.errors import AppError
from app.core.pagination import PageParams, parse_sort
from app.db import AuditLog
from app.modules.m10_audit import catalog, repo
from app.modules.m10_audit.schemas import (
    AuditFilters,
    AuditLogDetail,
    AuditLogItem,
    AuditPage,
    SecurityEventsPage,
)

DEFAULT_WINDOW = timedelta(days=30)
SECURITY_WINDOW = timedelta(days=7)
DEFAULT_SORT = "created_at:desc"


def resolve_window(
    filters: AuditFilters, default: timedelta = DEFAULT_WINDOW, now: datetime | None = None
) -> repo.Window:
    """Always bounded below, so partition pruning applies."""
    now = now or datetime.now(UTC)
    date_from = filters.date_from or (filters.date_to or now) - default
    return repo.Window(date_from=date_from, date_to=filters.date_to)


def to_criteria(filters: AuditFilters, window: repo.Window) -> repo.Criteria:
    return repo.Criteria(
        window=window,
        actor_user_id=filters.actor_user_id,
        actor_type=filters.actor_type,
        actions=tuple(filters.action or ()),
        entity_type=filters.entity_type,
        entity_id=filters.entity_id,
        result=filters.result,
        q=filters.q.strip() if filters.q and filters.q.strip() else None,
    )


def _redact_json(value: Any) -> Any:
    return audit.redact(value) if value is not None else None


def to_item(row: AuditLog) -> AuditLogItem:
    return AuditLogItem(
        id=row.id,
        created_at=row.created_at,
        org_id=row.org_id,
        actor_type=row.actor_type,
        actor_user_id=row.actor_user_id,
        actor_name=row.actor_name_snapshot,
        actor_role=row.actor_role,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        result=row.result,
        reason=row.reason,
        ip=row.ip,
        request_id=row.request_id,
    )


def to_detail(row: AuditLog) -> AuditLogDetail:
    return AuditLogDetail(
        **to_item(row).model_dump(),
        user_agent=row.user_agent,
        before=_redact_json(row.before),
        after=_redact_json(row.after),
    )


class AuditQuery:
    """Read interface of the audit log: ``AuditQuery(session).search(ctx, filters, page)``."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def scope_org(ctx: TenantContext, filters: AuditFilters | None = None) -> UUID | None:
        if ctx.is_platform:
            return getattr(filters, "org_id", None)
        if ctx.org_id is None:
            raise AppError("FORBIDDEN")
        return ctx.org_id

    async def search(self, ctx: TenantContext, filters: AuditFilters, page: PageParams) -> AuditPage:
        window = resolve_window(filters)
        sort = parse_sort(page.sort, set(repo.SORT_FIELDS), DEFAULT_SORT)
        rows, total = await repo.search(
            self.session,
            self.scope_org(ctx, filters),
            to_criteria(filters, window),
            sort,
            page.limit,
            page.offset,
        )
        return AuditPage(
            items=[to_item(r) for r in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
            date_from=window.date_from,
            date_to=window.date_to,
        )

    async def detail(self, ctx: TenantContext, log_id: UUID) -> AuditLogDetail:
        row = await repo.get(self.session, self.scope_org(ctx), log_id)
        if row is None:
            raise AppError("NOT_FOUND")
        return to_detail(row)

    async def security_events(
        self, ctx: TenantContext, filters: AuditFilters, page: PageParams
    ) -> SecurityEventsPage:
        """Failed logins and lockouts (the ``security_events`` gadget source). Default window: 7 days."""
        window = resolve_window(filters, SECURITY_WINDOW)
        org_id = self.scope_org(ctx, filters)
        criteria = to_criteria(filters.model_copy(update={"action": None}), window)
        criteria = replace(criteria, actions=catalog.SECURITY_ACTIONS)
        sort = parse_sort(page.sort, set(repo.SORT_FIELDS), DEFAULT_SORT)
        rows, total = await repo.search(self.session, org_id, criteria, sort, page.limit, page.offset)
        counts = await repo.count_by_action(self.session, org_id, criteria)
        return SecurityEventsPage(
            items=[to_item(r) for r in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
            date_from=window.date_from,
            date_to=window.date_to,
            failed_logins=counts.get(catalog.FAILED_LOGIN, 0),
            lockouts=counts.get(catalog.ACCOUNT_LOCKED, 0),
        )

    async def fetch_for_export(
        self, ctx: TenantContext, filters: AuditFilters, limit: int
    ) -> tuple[list[AuditLog], repo.Window]:
        """Up to ``limit`` rows, newest first (callers pass cap + 1 to detect overflow)."""
        window = resolve_window(filters)
        rows = await repo.fetch(
            self.session,
            self.scope_org(ctx, filters),
            to_criteria(filters, window),
            [("created_at", "desc")],
            limit,
        )
        return rows, window
