"""M10 audit routes (spec 6/M10): org admin view of the own organization under ``/audit-logs`` and
the read-only super admin equivalents under ``/platform/audit-logs``. No route here writes audit
rows except the export itself (``AUDIT_EXPORTED``)."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.errors import ERROR_CODES, validation_error
from app.core.pagination import PageParams, page_params
from app.core.roles import ORG_ADMIN, SUPER_ADMIN
from app.modules.m10_audit import catalog, export
from app.modules.m10_audit.schemas import (
    ActionCatalog,
    ActionGroup,
    ActionItem,
    AuditFilters,
    AuditLogDetail,
    AuditPage,
    ColumnCatalog,
    ExportRequest,
    PlatformAuditFilters,
    PlatformExportRequest,
    SecurityEventsPage,
)
from app.modules.m10_audit.service import AuditQuery

_OA = require_role(ORG_ADMIN)
_SA = require_role(SUPER_ADMIN)
OrgAdmin = Annotated[TenantContext, Depends(_OA)]
SuperAdmin = Annotated[TenantContext, Depends(_SA)]
Paging = Annotated[PageParams, Depends(page_params)]

org_router = APIRouter(
    prefix="/api/v1/audit-logs",
    tags=["audit"],
    dependencies=[Depends(_OA), Depends(require_feature(flags.AUDIT_VIEW))],
)
platform_router = APIRouter(
    prefix="/api/v1/platform/audit-logs", tags=["audit (platform)"], dependencies=[Depends(_SA)]
)

# --------------------------------------------------------------------------- docs helpers


def _error(code: str, description: str, details: dict | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message_key": ERROR_CODES[code].message_key,
                        "details": details or {},
                        "request_id": "7c1f0c52-0b1e-4f5e-9d38-2f0f6a1d1a11",
                    }
                }
            }
        },
    }


def _errors(*extra: tuple[int, str, str, dict | None], flag: str | None = None) -> dict[int | str, dict]:
    out: dict[int | str, dict] = {
        401: _error("UNAUTHENTICATED", "Missing or invalid access token"),
        403: _error(
            "FORBIDDEN", "Role not allowed for this route (or the feature flag is off: FEATURE_DISABLED)"
        ),
    }
    for status, code, description, details in extra:
        out[status] = _error(code, description, details)
    return out


_VALIDATION = (
    422,
    "VALIDATION_ERROR",
    "Invalid filter, sort, page size or column",
    {"fields": [{"field": "action", "code": "unknown_action"}]},
)
_NOT_FOUND = (404, "NOT_FOUND", "No such audit row in the caller's scope", None)

_ITEM_EXAMPLE = {
    "id": "0190f3a2-7b1c-7a55-9c1e-1c2d3e4f5a6b",
    "created_at": "2026-11-16T09:30:12.481Z",
    "org_id": "5f0f3c2e-8d2a-4b53-8f55-0d1a2b3c4d5e",
    "actor_type": "user",
    "actor_user_id": "a1b2c3d4-0000-4000-8000-000000000001",
    "actor_name": "Priya Nair",
    "actor_role": "org_admin",
    "action": "USER_CREATED",
    "entity_type": "user",
    "entity_id": "a1b2c3d4-0000-4000-8000-000000000002",
    "result": "success",
    "reason": None,
    "ip": "203.0.113.7",
    "request_id": "7c1f0c52-0b1e-4f5e-9d38-2f0f6a1d1a11",
}
_LIST_EXAMPLE = {
    "items": [_ITEM_EXAMPLE],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "date_from": "2026-10-17T09:30:12Z",
    "date_to": None,
}
_DETAIL_EXAMPLE = {
    **_ITEM_EXAMPLE,
    "user_agent": "Mozilla/5.0",
    "before": None,
    "after": {"email": "new.hr@example.com", "password_hash": "[REDACTED]"},
}


def _ok(model: Any, example: dict, description: str) -> dict[int | str, dict]:
    return {
        200: {
            "model": model,
            "description": description,
            "content": {"application/json": {"example": example}},
        }
    }


_EXPORT_OK: dict[int | str, dict] = {
    200: {
        "description": (
            "The file (`Content-Disposition: attachment`). The export is itself audited (`AUDIT_EXPORTED`)."
        ),
        "content": {
            "text/csv": {
                "example": "created_at,actor_name,action\n2026-11-16T09:30:12+00:00,Priya Nair,USER_CREATED\n"
            },
            "application/pdf": {},
        },
    }
}

# --------------------------------------------------------------------------- filter dependencies


def _build(model: type[AuditFilters], **values: Any) -> Any:
    try:
        return model(**values)
    except ValidationError as exc:
        fields = [
            {
                "field": str(err["loc"][-1]) if err["loc"] else "filters",
                "code": str(err["msg"]).removeprefix("Value error, "),
            }
            for err in exc.errors()
        ]
        # A cross-field error has no location: report it on the field that is out of order.
        fields = [{**f, "field": "date_to"} if f["code"] == "date_range_invalid" else f for f in fields]
        raise validation_error(fields) from exc


def audit_filters(
    actor_user_id: Annotated[str | None, Query(description="Acting user id")] = None,
    actor_type: Annotated[str | None, Query(description="user, candidate or system")] = None,
    action: Annotated[list[str] | None, Query(description="Action name(s), repeatable (Appendix B)")] = None,
    entity_type: Annotated[str | None, Query(max_length=100)] = None,
    entity_id: Annotated[str | None, Query(max_length=200)] = None,
    result: Annotated[str | None, Query(description="success, failure or denied")] = None,
    date_from: Annotated[
        datetime | None, Query(description="Inclusive ISO 8601; default is 30 days back")
    ] = None,
    date_to: Annotated[datetime | None, Query(description="Inclusive ISO 8601")] = None,
    q: Annotated[
        str | None, Query(max_length=200, description="Text search: actor, action, entity, reason")
    ] = None,
) -> AuditFilters:
    return _build(
        AuditFilters,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        result=result,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )


def platform_audit_filters(
    base: Annotated[AuditFilters, Depends(audit_filters)],
    org_id: Annotated[UUID | None, Query(description="Restrict to one organization")] = None,
) -> PlatformAuditFilters:
    return _build(PlatformAuditFilters, **base.model_dump(exclude_none=True), org_id=org_id)


Filters = Annotated[AuditFilters, Depends(audit_filters)]
PlatformFilters = Annotated[PlatformAuditFilters, Depends(platform_audit_filters)]

# --------------------------------------------------------------------------- shared handlers


def _actions() -> ActionCatalog:
    return ActionCatalog(
        groups=[
            ActionGroup(
                key=key,
                label_key=f"audit.groups.{key}",
                actions=[ActionItem(action=a, label_key=f"audit.actions.{a}") for a in actions],
            )
            for key, actions in catalog.ACTION_GROUPS
        ]
    )


def _file(result: export.ExportResult) -> Response:
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Row-Count": str(result.row_count),
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- org admin routes


@org_router.get(
    "",
    response_model=AuditPage,
    summary="Search the organization's audit log",
    description=(
        "Filters: actor, action(s), entity, result, text and a date range (ISO 8601 UTC). Without "
        "`date_from` the window is the last 30 days (the table is partitioned monthly, so a bounded range "
        "is also what keeps the query fast). Before and after values are on the detail route. "
        "Sort with `sort=created_at|action|actor_name|result:asc|desc` (default `created_at:desc`)."
    ),
    responses={**_ok(AuditPage, _LIST_EXAMPLE, "One page of audit rows"), **_errors(_VALIDATION)},
)
async def list_audit_logs(ctx: OrgAdmin, session: DbSession, filters: Filters, page: Paging) -> AuditPage:
    return await AuditQuery(session).search(ctx, filters, page)


@org_router.get(
    "/actions",
    response_model=ActionCatalog,
    summary="Action catalog for the audit filters",
    description="Every audit action name (spec Appendix B) grouped by area, with label message keys.",
    responses={
        **_ok(
            ActionCatalog,
            {
                "groups": [
                    {
                        "key": "auth",
                        "label_key": "audit.groups.auth",
                        "actions": [
                            {"action": "AUTH_LOGIN_FAILED", "label_key": "audit.actions.AUTH_LOGIN_FAILED"}
                        ],
                    }
                ]
            },
            "The catalog",
        ),
        **_errors(),
    },
)
async def list_actions(ctx: OrgAdmin) -> ActionCatalog:
    return _actions()


@org_router.get(
    "/columns",
    response_model=ColumnCatalog,
    summary="Exportable columns",
    description="Column keys accepted by the export, with header message keys and the default selection.",
    responses={
        **_ok(
            ColumnCatalog,
            {"items": [{"key": "created_at", "label_key": "audit.columns.created_at", "default": True}]},
            "The columns",
        ),
        **_errors(),
    },
)
async def list_columns(ctx: OrgAdmin) -> ColumnCatalog:
    return ColumnCatalog(items=export.column_catalog())


@org_router.get(
    "/security-events",
    response_model=SecurityEventsPage,
    summary="Failed logins and account lockouts",
    description=(
        "Source of the `security_events` dashboard gadget: `AUTH_LOGIN_FAILED` and `AUTH_ACCOUNT_LOCKED` "
        "rows of the own organization, newest first, with counts for the window (default last 7 days)."
    ),
    responses={
        **_ok(SecurityEventsPage, {**_LIST_EXAMPLE, "failed_logins": 3, "lockouts": 1}, "Events and counts"),
        **_errors(_VALIDATION),
    },
)
async def security_events(
    ctx: OrgAdmin, session: DbSession, filters: Filters, page: Paging
) -> SecurityEventsPage:
    return await AuditQuery(session).security_events(ctx, filters, page)


@org_router.get(
    "/{log_id}",
    response_model=AuditLogDetail,
    summary="Audit row detail with before and after",
    description="Redacted before and after values (`[REDACTED]` replaces secrets). Other organizations: 404.",
    responses={**_ok(AuditLogDetail, _DETAIL_EXAMPLE, "The audit row"), **_errors(_NOT_FOUND, _VALIDATION)},
)
async def get_audit_log(log_id: UUID, ctx: OrgAdmin, session: DbSession) -> AuditLogDetail:
    return await AuditQuery(session).detail(ctx, log_id)


@org_router.post(
    "/export",
    response_class=Response,
    dependencies=[Depends(require_feature(flags.AUDIT_EXPORT))],
    summary="Export the filtered audit log as CSV or PDF",
    description=(
        "Same filters as the search, a chosen column list (see `/audit-logs/columns`) and a format. "
        "CSV cells "
        "that start with `= + - @` are prefixed with an apostrophe (formula injection guard); the PDF "
        "header carries the application name. The export is itself audited (`AUDIT_EXPORTED`: format, "
        "columns, filters, row count). Limits: 50000 rows for CSV, 2000 for PDF (otherwise narrow the "
        "filters). Needs flags `audit_view` and `audit_export`."
    ),
    responses={
        **_EXPORT_OK,
        **_errors(
            (
                422,
                "VALIDATION_ERROR",
                "Unknown column or too many rows",
                {"fields": [{"field": "filters", "code": "too_many_rows"}], "max_rows": 50000},
            )
        ),
    },
)
async def export_audit_logs(body: ExportRequest, ctx: OrgAdmin, session: DbSession) -> Response:
    return _file(await export.export_audit(ctx, session, body.filters, body.columns, body.format))


# --------------------------------------------------------------------------- super admin routes


@platform_router.get(
    "",
    response_model=AuditPage,
    summary="Search audit logs across all organizations (read-only)",
    description=(
        "Super admin view of every organization plus platform events (`org_id` null). Same filters as "
        "`GET /audit-logs`, plus `org_id`. Read-only: there is no write route."
    ),
    responses={**_ok(AuditPage, _LIST_EXAMPLE, "One page of audit rows"), **_errors(_VALIDATION)},
)
async def platform_list(
    ctx: SuperAdmin, session: DbSession, filters: PlatformFilters, page: Paging
) -> AuditPage:
    return await AuditQuery(session).search(ctx, filters, page)


@platform_router.get(
    "/actions",
    response_model=ActionCatalog,
    summary="Action catalog for the platform audit filters",
    description="Every audit action name (spec Appendix B) grouped by area.",
    responses={**_ok(ActionCatalog, {"groups": []}, "The catalog"), **_errors()},
)
async def platform_actions(ctx: SuperAdmin) -> ActionCatalog:
    return _actions()


@platform_router.get(
    "/columns",
    response_model=ColumnCatalog,
    summary="Exportable columns for the platform export",
    description="Column keys accepted by the platform export.",
    responses={**_ok(ColumnCatalog, {"items": []}, "The columns"), **_errors()},
)
async def platform_columns(ctx: SuperAdmin) -> ColumnCatalog:
    return ColumnCatalog(items=export.column_catalog())


@platform_router.get(
    "/{log_id}",
    response_model=AuditLogDetail,
    summary="Audit row detail across organizations (read-only)",
    description="Redacted before and after values of any organization's row (or a platform event).",
    responses={**_ok(AuditLogDetail, _DETAIL_EXAMPLE, "The audit row"), **_errors(_NOT_FOUND, _VALIDATION)},
)
async def platform_get(log_id: UUID, ctx: SuperAdmin, session: DbSession) -> AuditLogDetail:
    return await AuditQuery(session).detail(ctx, log_id)


@platform_router.post(
    "/export",
    response_class=Response,
    summary="Export audit logs across organizations as CSV or PDF",
    description=(
        "Super admin export with the same rules as the organization export (injection guard, row caps, "
        "`AUDIT_EXPORTED` entry with a null organization). Not flag-gated."
    ),
    responses={
        **_EXPORT_OK,
        **_errors(
            (
                422,
                "VALIDATION_ERROR",
                "Unknown column or too many rows",
                {"fields": [{"field": "columns", "code": "unknown_column"}]},
            )
        ),
    },
)
async def platform_export(body: PlatformExportRequest, ctx: SuperAdmin, session: DbSession) -> Response:
    return _file(await export.export_audit(ctx, session, body.filters, body.columns, body.format))


# A single ``router`` is what ``app.main`` discovers. Static paths (``/actions`` ...) are declared
# before ``/{log_id}`` so they win the match.
router = APIRouter()
router.include_router(org_router)
router.include_router(platform_router)
