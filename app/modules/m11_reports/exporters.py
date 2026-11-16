"""CSV (formula-injection safe) and PDF (header from the application name) output (spec 7.4 step 7)."""

import html
from typing import Any

from app.core.config import settings
from app.core.export import csv_bytes, render_pdf
from app.modules.m11_reports.executor import display

CSV_ROW_CAP = 50_000
PDF_ROW_CAP = 2_000
CAPS = {"csv": CSV_ROW_CAP, "pdf": PDF_ROW_CAP}
CONTENT_TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}


def build_csv(columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    return csv_bytes(columns, ([display(r.get(c)) for c in columns] for r in rows))


def pdf_html(title: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape('' if display(r.get(c)) is None else str(display(r.get(c))))}</td>"
            for c in columns
        )
        + "</tr>"
        for r in rows
    )
    return (
        "<style>body{font-family:sans-serif;font-size:10px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #bbb;padding:3px;text-align:left}th{background:#eee}</style>"
        f"<h1>{html.escape(settings.app_name)}</h1><h2>{html.escape(title)}</h2>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def build_pdf(title: str, columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    return render_pdf(title, pdf_html(title, columns, rows))
