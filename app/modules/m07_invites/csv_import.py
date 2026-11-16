"""CSV parsing for bulk invites (RF-5): BOM, malformed rows, in-file duplicates, 200-row cap, formulas."""

import csv
import io
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from app.core.errors import validation_error
from app.core.security import blind_index
from app.modules.m07_invites.schemas import Email, Phone

MAX_ROWS = 200
MAX_BYTES = 512 * 1024
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_EMAIL = TypeAdapter(Email)
_PHONE = TypeAdapter(Phone)
_HEADERS = {"name", "email", "phone"}


@dataclass
class CsvRow:
    row: int  # 1-based data row (the header is row 0)
    name: str = ""
    email: str = ""
    phone: str | None = None
    error: str | None = None


def parse(data: bytes) -> list[CsvRow]:
    if len(data) > MAX_BYTES:
        raise validation_error([{"field": "file", "code": "too_large"}])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise validation_error([{"field": "file", "code": "not_utf8"}]) from None
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except (StopIteration, csv.Error):
        raise validation_error([{"field": "file", "code": "empty_file"}]) from None
    if not {"name", "email"} <= set(header) or not set(header) <= _HEADERS:
        raise validation_error([{"field": "file", "code": "bad_header"}])
    index = {h: i for i, h in enumerate(header)}
    rows: list[CsvRow] = []
    seen: set[str] = set()
    try:
        for raw in reader:
            if not any(c.strip() for c in raw):
                continue  # blank line
            if len(rows) >= MAX_ROWS:
                raise validation_error([{"field": "file", "code": "too_many_rows", "max": MAX_ROWS}])
            row = CsvRow(row=len(rows) + 1)
            rows.append(row)
            get = lambda k, raw=raw: raw[index[k]].strip() if k in index and index[k] < len(raw) else ""  # noqa: E731
            row.name, row.email, phone = get("name"), get("email"), get("phone")
            row.error = _check(row, phone, seen)
            if not row.error and phone:
                row.phone = phone
    except csv.Error:
        raise validation_error([{"field": "file", "code": "malformed_csv"}]) from None
    return rows


def _check(row: CsvRow, phone: str, seen: set[str]) -> str | None:
    if len(row.name) > 200 or not row.name:
        return "name_invalid"
    if row.name.startswith(_FORMULA) or row.email.startswith(_FORMULA[:1] + _FORMULA[2:]):
        return "formula_not_allowed"
    try:
        row.email = str(_EMAIL.validate_python(row.email))
    except ValidationError:
        return "email_invalid"
    if phone:
        try:
            _PHONE.validate_python(phone)
        except ValidationError:
            return "phone_invalid"
    key = blind_index(row.email)
    if key in seen:
        return "duplicate_in_file"
    seen.add(key)
    return None


__all__ = ["MAX_ROWS", "CsvRow", "parse"]
