"""Run compiled queries on the read-only role (spec 7.4 step 5): read-only transaction, statement timeout,
row cap. The count shown at confirmation comes from here, never from the AI."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import security
from app.core.db import TenantContext, reports_session
from app.core.errors import AppError
from app.modules.m11_reports.compiler import Compiled

TIMEOUT_MS = 15_000


def _guard(exc: DBAPIError) -> AppError:
    if "statement timeout" in str(exc.orig):
        return AppError("REPORT_FILTER_INVALID", details={"fields": [{"field": "query", "code": "too_slow"}]})
    raise exc


async def run_readonly(
    ctx: TenantContext, sql: str, params: dict[str, Any], *, timeout_ms: int = TIMEOUT_MS
) -> list[dict[str, Any]]:
    """Any statement that writes fails: the transaction is READ ONLY and the role has SELECT only."""
    try:
        async with reports_session(ctx, statement_timeout_ms=timeout_ms) as session:
            return [dict(r._mapping) for r in await session.execute(text(sql), params)]
    except DBAPIError as exc:
        raise _guard(exc) from exc


async def count_rows(ctx: TenantContext, compiled: Compiled) -> int:
    count_params = {k: v for k, v in compiled.params.items() if k != "row_limit"}
    return int((await run_readonly(ctx, compiled.count_sql, count_params))[0]["n"])


def decrypt_pii(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    """Candidate email is decrypted at generation time only, for the requesting admin (spec 7.4.9)."""
    for row in rows:
        for column in columns:
            blob = row.get(column)
            try:
                row[column] = security.decrypt_field(blob) if blob else None
            except ValueError:
                row[column] = None


def display(value: Any) -> Any:
    """Cell value for CSV and PDF: UTC timestamps, plain numbers (a Decimal would be text to ``csv_safe``)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
