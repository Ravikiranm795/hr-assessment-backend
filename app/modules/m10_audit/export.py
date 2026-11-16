"""Audit export (spec 6/M10, 4.5): CSV (formula-injection guarded) and PDF, itself audited.

``export_audit`` runs the same filters as the audit screen, renders the chosen columns and writes
an ``AUDIT_EXPORTED`` entry (format, columns, filters, row count; never the rows) in the same
transaction. Values are the stored, already redacted ones. CSV cells go through
``core.export.csv_safe`` (via ``csv_bytes``); PDF cells are HTML-escaped; the PDF header comes from
``settings.app_name``.
"""

import html
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.export import csv_bytes, render_pdf
from app.db import AuditLog
from app.modules.m10_audit.schemas import AuditFilters, ColumnItem
from app.modules.m10_audit.service import AuditQuery, to_detail

# Row caps keep memory and render time bounded; over the cap the caller narrows the filters.
MAX_EXPORT_ROWS: dict[str, int] = {"csv": 50_000, "pdf": 2_000}
PDF_CELL_MAX_CHARS = 300

MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}


def _json_cell(value: Any) -> str:
    return "" if value is None else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


@dataclass(frozen=True)
class ExportColumn:
    key: str
    label: str  # readable header for the PDF (the CSV header row uses the key)
    default: bool
    value: Callable[[Any], Any]  # AuditLogDetail -> cell

    @property
    def label_key(self) -> str:
        return f"audit.columns.{self.key}"


COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("created_at", "Time (UTC)", True, lambda r: r.created_at.astimezone(UTC).isoformat()),
    ExportColumn("actor_name", "Actor", True, lambda r: r.actor_name),
    ExportColumn("actor_role", "Role", True, lambda r: r.actor_role),
    ExportColumn("actor_type", "Actor type", False, lambda r: r.actor_type),
    ExportColumn("actor_user_id", "Actor id", False, lambda r: r.actor_user_id),
    ExportColumn("action", "Action", True, lambda r: r.action),
    ExportColumn("entity_type", "Entity type", True, lambda r: r.entity_type),
    ExportColumn("entity_id", "Entity id", True, lambda r: r.entity_id),
    ExportColumn("result", "Result", True, lambda r: r.result),
    ExportColumn("reason", "Reason", False, lambda r: r.reason),
    ExportColumn("before", "Before", False, lambda r: _json_cell(r.before)),
    ExportColumn("after", "After", False, lambda r: _json_cell(r.after)),
    ExportColumn("ip", "IP", False, lambda r: r.ip),
    ExportColumn("user_agent", "Device", False, lambda r: r.user_agent),
    ExportColumn("request_id", "Request id", False, lambda r: r.request_id),
    ExportColumn("org_id", "Organization id", False, lambda r: r.org_id),
    ExportColumn("id", "Audit id", False, lambda r: r.id),
)
_BY_KEY = {c.key: c for c in COLUMNS}


def column_catalog() -> list[ColumnItem]:
    return [ColumnItem(key=c.key, label_key=c.label_key, default=c.default) for c in COLUMNS]


def resolve_columns(requested: Sequence[str] | None) -> list[str]:
    """Validated, de-duplicated column keys in the requested order (defaults when empty)."""
    if not requested:
        return [c.key for c in COLUMNS if c.default]
    unknown = [k for k in requested if k not in _BY_KEY]
    if unknown:
        raise validation_error([{"field": "columns", "code": "unknown_column"}])
    return list(dict.fromkeys(requested))


def _cell(value: Any) -> Any:
    return str(value) if value is not None and not isinstance(value, str | int | float | bool) else value


def build_rows(rows: Sequence[AuditLog], columns: Sequence[str]) -> list[dict[str, Any]]:
    """One ``{column: cell}`` dict per row, from the redacted detail view of each audit row."""
    details = [to_detail(r) for r in rows]
    return [{key: _cell(_BY_KEY[key].value(d)) for key in columns} for d in details]


def _pdf_html(rows: list[dict[str, Any]], columns: Sequence[str], filters_summary: str) -> str:
    esc = html.escape
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    head = "".join(f"<th>{esc(_BY_KEY[k].label)}</th>" for k in columns)

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        if len(text) > PDF_CELL_MAX_CHARS:
            text = text[:PDF_CELL_MAX_CHARS] + "..."
        return f"<td>{esc(text)}</td>"

    body = "".join("<tr>" + "".join(cell(row[k]) for k in columns) + "</tr>" for row in rows)
    return (
        f"<h1>{esc(settings.app_name)}</h1><h2>Audit log export</h2>"
        f"<p>Generated {esc(generated)} &middot; {len(rows)} rows &middot; {esc(filters_summary)}</p>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def render_export(
    rows: list[dict[str, Any]], columns: Sequence[str], fmt: str, filters_summary: str = ""
) -> bytes:
    if fmt == "csv":
        return csv_bytes(list(columns), ([row[k] for k in columns] for row in rows))
    return render_pdf(f"{settings.app_name} audit log", _pdf_html(rows, columns, filters_summary))


@dataclass(frozen=True)
class ExportResult:
    content: bytes
    media_type: str
    filename: str
    row_count: int


def _filters_json(filters: AuditFilters, window_from: datetime, window_to: datetime | None) -> dict[str, Any]:
    out = filters.model_dump(mode="json", exclude_none=True)
    out["date_from"] = window_from.astimezone(UTC).isoformat()  # the effective window
    if window_to is not None:
        out["date_to"] = window_to.astimezone(UTC).isoformat()
    return out


async def export_audit(
    ctx: TenantContext,
    session: AsyncSession,
    filters: AuditFilters,
    columns: Sequence[str] | None,
    fmt: str,
) -> ExportResult:
    """Run the filtered query, render it and audit the export (``AUDIT_EXPORTED``)."""
    keys = resolve_columns(columns)
    cap = MAX_EXPORT_ROWS[fmt]
    rows, window = await AuditQuery(session).fetch_for_export(ctx, filters, cap + 1)
    if len(rows) > cap:
        raise AppError(
            "VALIDATION_ERROR",
            422,
            {"fields": [{"field": "filters", "code": "too_many_rows"}], "max_rows": cap},
        )
    applied = _filters_json(filters, window.date_from, window.date_to)
    cells = build_rows(rows, keys)
    summary = ", ".join(f"{k}={v}" for k, v in applied.items())
    content = render_export(cells, keys, fmt, summary)
    await audit.record(
        ctx,
        session,
        audit.AUDIT_EXPORTED,
        "audit_log",
        None,
        after={"format": fmt, "columns": keys, "filters": applied, "row_count": len(rows)},
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return ExportResult(content, MEDIA_TYPES[fmt], f"audit-log-{stamp}.{fmt}", len(rows))
