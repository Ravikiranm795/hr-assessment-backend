"""M11 AI report chat routes (spec 6/M11). Org admin under ``/reports`` (flag ``reports_chat``); super admin
under ``/platform/reports`` (organization-level aggregates and audit only, no candidate content)."""

# ruff: noqa: E501  # (Swagger description text)

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.errors import ERROR_CODES
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import ORG_ADMIN, SUPER_ADMIN
from app.modules.m11_reports.schemas import (
    CatalogOut,
    ChatOut,
    CountOut,
    GenerateIn,
    InterpretIn,
    ReportOut,
    SelectIn,
)
from app.modules.m11_reports.service import ReportService

_OA = require_role(ORG_ADMIN)
_SA = require_role(SUPER_ADMIN)
OrgAdmin = Annotated[TenantContext, Depends(_OA)]
SuperAdmin = Annotated[TenantContext, Depends(_SA)]
Paging = Annotated[PageParams, Depends(page_params)]

RID = "0190f3a2-7b1c-7a55-9c1e-1c2d3e4f5a6b"
SPEC = {
    "source": "candidates",
    "filters": [
        {"field": "decision", "op": "eq", "value": "rejected"},
        {"field": "submitted_at", "op": "between", "value": ["2026-10-01", "2026-10-31"]},
    ],
    "columns": ["candidate_name", "candidate_email", "role_title", "score_percent", "decided_at"],
    "sort": [{"field": "submitted_at", "dir": "desc"}],
    "aggregate": None,
    "limit": 50000,
}
REPORT = {
    "id": RID,
    "source": "candidates",
    "user_text": "Rejected candidates from October with their scores",
    "status": "confirmed",
    "filter_spec": SPEC,
    "columns": SPEC["columns"],
    "format": None,
    "row_count": None,
    "file_available": False,
    "created_by": "a1b2c3d4-0000-4000-8000-000000000001",
    "created_at": "2026-11-16T09:30:12.481Z",
}


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


def _errors(*extra: tuple[int, str, str, dict | None]) -> dict[int | str, dict]:
    out: dict[int | str, dict] = {
        401: _error("UNAUTHENTICATED", "Missing or invalid access token"),
        403: _error("FORBIDDEN", "Role not allowed (HR users are forbidden), or flag off (FEATURE_DISABLED)"),
    }
    for status, code, description, details in extra:
        out[status] = _error(code, description, details)
    return out


def _ok(model: Any, example: dict, description: str) -> dict[int | str, dict]:
    return {
        200: {
            "model": model,
            "description": description,
            "content": {"application/json": {"example": example}},
        }
    }


FILTER_INVALID = (
    422,
    "REPORT_FILTER_INVALID",
    "Unknown source, field, operator or value, or the row cap was exceeded",
    {"fields": [{"field": "filters.0.field", "code": "unknown_field"}]},
)
VALIDATION = (
    422,
    "VALIDATION_ERROR",
    "Malformed body",
    {"fields": [{"field": "text", "code": "string_too_short"}]},
)
NOT_FOUND = (404, "NOT_FOUND", "No such report request in the caller's scope", None)
CONFLICT = (
    409,
    "CONFLICT",
    "The request is not in the right state for this step",
    {"reason": "not_confirmed"},
)
AI_ERRORS = (
    (
        422,
        "AI_NOT_CONFIGURED",
        "The organization has no usable AI provider (use `filter_spec` instead)",
        None,
    ),
    (502, "AI_PROVIDER_ERROR", "The provider failed or timed out", None),
)
CHAT_EXAMPLE = {
    "report": {**REPORT, "status": "interpreted", "filter_spec": None},
    "interpretations": [
        {
            "index": 0,
            "label": "Rejected candidates who submitted in October 2026",
            "filter_spec": SPEC,
            "columns": SPEC["columns"],
            "estimated_rows": 42,
        }
    ],
    "clarifying_question": None,
    "estimated_rows": None,
}
FILE_OK: dict[int | str, dict] = {
    200: {
        "description": "The file (`Content-Disposition: attachment`). The download is audited (`REPORT_DOWNLOADED`).",
        "content": {
            "text/csv": {"example": "candidate_name,role_title\nAsha Rao,Data Analyst\n"},
            "application/pdf": {},
        },
    }
}


def build(router: APIRouter, guard: Any, who: str) -> None:
    """Register the seven report-chat routes on ``router`` for the guard's role."""
    Ctx = Annotated[TenantContext, Depends(guard)]  # noqa: N806
    sa = who == "platform"
    scope = (
        "organization-level aggregates and audit only; no candidate content"
        if sa
        else "the caller's organization only"
    )

    @router.get(
        "/catalog",
        response_model=CatalogOut,
        summary="Report field catalog",
        description=(
            f"Sources, fields, types, operators and allowed values ({scope}). This is exactly what the AI is "
            "given, and it drives the report UI. It contains no data rows."
        ),
        responses={
            **_ok(
                CatalogOut,
                {
                    "sources": [
                        {
                            "source": "candidates",
                            "default_columns": ["candidate_name", "role_title"],
                            "fields": [
                                {
                                    "name": "decision",
                                    "type": "enum",
                                    "label_key": "reports.fields.decision",
                                    "ops": ["eq", "in"],
                                    "pii": False,
                                    "sortable": True,
                                    "groupable": True,
                                    "values": ["waiting", "approved", "rejected"],
                                }
                            ],
                        }
                    ]
                },
                "The catalog",
            ),
            **_errors(),
        },
    )
    async def catalog(ctx: Ctx) -> CatalogOut:
        return ReportService.catalog(ctx)

    @router.post(
        "/chat/interpret",
        response_model=ChatOut,
        summary="Interpret a report question",
        description=(
            "Free text (and an optional source) becomes up to four interpretations, each with a readable label, "
            "the structured FilterSpec, suggested columns and an estimated row count counted in the database; or "
            "a clarifying question. The AI sees only the field catalog and your words, never a row. Every spec is "
            "validated against the catalog (`REPORT_FILTER_INVALID` when the AI proposed only invalid ones). "
            "Instead of `text`, a `filter_spec` can be sent to skip the AI (the only way for super admin reports). "
            f"Scope: {scope}. Audited as `REPORT_INTERPRETED` (the text and the specs, never rows)."
        ),
        responses={
            **_ok(ChatOut, CHAT_EXAMPLE, "The interpretations"),
            **_errors(FILTER_INVALID, VALIDATION, *AI_ERRORS),
        },
    )
    async def interpret(body: InterpretIn, ctx: Ctx, session: DbSession) -> ChatOut:
        return await ReportService.interpret(ctx, session, body)

    @router.post(
        "/chat/select",
        response_model=ChatOut,
        summary="Confirm an interpretation or refine the question",
        description=(
            "Send `index` to confirm one interpretation (status becomes `confirmed`; the estimated rows are "
            "recounted in the database), or `text` to refine, which returns new interpretations for the same "
            "request."
        ),
        responses={
            **_ok(
                ChatOut,
                {"report": REPORT, "interpretations": [], "clarifying_question": None, "estimated_rows": 42},
                "The confirmed request",
            ),
            **_errors(NOT_FOUND, CONFLICT, FILTER_INVALID, VALIDATION, *AI_ERRORS),
        },
    )
    async def select(body: SelectIn, ctx: Ctx, session: DbSession) -> ChatOut:
        return await ReportService.select(ctx, session, body)

    @router.post(
        "/{report_id}/preview-count",
        response_model=CountOut,
        summary="Row count only",
        description="Counts the rows the confirmed filter matches (capped by the spec's limit). Nothing is generated.",
        responses={
            **_ok(CountOut, {"id": RID, "row_count": 42}, "The count"),
            **_errors(NOT_FOUND, CONFLICT, FILTER_INVALID),
        },
    )
    async def preview_count(report_id: uuid.UUID, ctx: Ctx, session: DbSession) -> CountOut:
        return await ReportService.preview_count(ctx, session, report_id)

    @router.post(
        "/{report_id}/generate",
        response_model=ReportOut,
        summary="Run the approved query and build the file",
        description=(
            "Runs the confirmed filter on the read-only database role (read-only transaction, 15 s statement "
            "timeout) and builds a CSV (cells starting with `= + - @` are prefixed with an apostrophe) or a PDF "
            "(header with the application name). Row caps: 50000 for CSV, 2000 for PDF, otherwise "
            "`REPORT_FILTER_INVALID` (`row_cap_exceeded`). Candidate emails are decrypted only now. The file is "
            "kept encrypted for 7 days. Audited as `REPORT_GENERATED` (spec, format, count, never rows)."
        ),
        responses={
            **_ok(
                ReportOut,
                {**REPORT, "status": "generated", "format": "csv", "row_count": 42, "file_available": True},
                "The request",
            ),
            **_errors(NOT_FOUND, CONFLICT, FILTER_INVALID, VALIDATION),
        },
    )
    async def generate(report_id: uuid.UUID, body: GenerateIn, ctx: Ctx, session: DbSession) -> ReportOut:
        return await ReportService.generate(ctx, session, report_id, body)

    @router.get(
        "/{report_id}/download",
        response_class=Response,
        summary="Download the generated file",
        description="Returns the file. Audited as `REPORT_DOWNLOADED`. 404 when nothing was generated or the "
        "file was removed by the nightly cleanup (7 days).",
        responses={**FILE_OK, **_errors(NOT_FOUND)},
    )
    async def download(report_id: uuid.UUID, ctx: Ctx, session: DbSession) -> Response:
        content, media_type, filename = await ReportService.download(ctx, session, report_id)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        "",
        response_model=Page[ReportOut],
        summary="Report request history",
        description=f"Newest first ({scope}). Generated files are kept 7 days; the request row stays.",
        responses={
            **_ok(Page[ReportOut], {"items": [REPORT], "total": 1, "page": 1, "page_size": 20}, "One page"),
            **_errors(),
        },
    )
    async def history(ctx: Ctx, session: DbSession, page: Paging) -> Page[ReportOut]:
        return await ReportService.history(ctx, session, page)


org_router = APIRouter(
    prefix="/api/v1/reports",
    tags=["reports"],
    dependencies=[Depends(_OA), Depends(require_feature(flags.REPORTS_CHAT))],
)
platform_router = APIRouter(
    prefix="/api/v1/platform/reports", tags=["reports (platform)"], dependencies=[Depends(_SA)]
)
build(org_router, _OA, "org")
build(platform_router, _SA, "platform")

router = APIRouter()
router.include_router(org_router)
router.include_router(platform_router)
