"""CSV and PDF export helpers (spec 7.4 step 7, M10 audit export)."""

import csv
import html as html_lib
import io
from collections.abc import Iterable
from typing import Any

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(cell: Any) -> str:
    """Neutralize spreadsheet formula injection: text starting with ``= + - @`` (or tab/CR) gets
    a leading apostrophe. Numbers are left alone (``-5`` stays ``-5``); ``None`` becomes ``""``."""
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, int | float):
        return str(cell)
    text = str(cell)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def csv_bytes(headers: list[str], rows: Iterable[Iterable[Any]]) -> bytes:
    """UTF-8 CSV with BOM (opens correctly in Excel), every cell passed through ``csv_safe``."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([csv_safe(h) for h in headers])
    for row in rows:
        writer.writerow([csv_safe(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


def render_pdf(title: str, html: str) -> bytes:
    """Render an HTML body to PDF. ``html`` must already be sanitized/escaped by the caller;
    ``title`` is escaped here. Remote and file URLs are blocked (images must be ``data:`` URIs),
    so the renderer cannot be used for SSRF or local file reads; blocked resources are skipped."""
    from weasyprint import HTML  # heavy import, deferred
    from weasyprint.urls import URLFetcher

    document = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title>"
        "<style>body{font-family:sans-serif;font-size:11px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:3px}</style>"
        f"</head><body>{html}</body></html>"
    )
    fetcher = URLFetcher(allowed_protocols=("data",), allow_redirects=False, timeout=5)
    return HTML(string=document, url_fetcher=fetcher).write_pdf()
