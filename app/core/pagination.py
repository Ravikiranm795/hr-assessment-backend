"""Pagination and sorting (spec 4.6): ``page``, ``page_size`` (max 100), ``total``,
``sort=field:asc|desc`` (comma-separated for several keys)."""

from dataclasses import dataclass

from fastapi import Query
from pydantic import BaseModel

from app.core.errors import validation_error

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class PageParams:
    page: int
    page_size: int
    sort: str | None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def page_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None, max_length=200, description="field:asc|desc[,field:asc|desc]"),
) -> PageParams:
    """FastAPI dependency. ``page_size`` above 100 is rejected with ``VALIDATION_ERROR``."""
    return PageParams(page=page, page_size=page_size, sort=sort)


def parse_sort(sort: str | None, allowed: set[str], default: str | None = None) -> list[tuple[str, str]]:
    """Parse and whitelist a sort string. Raises ``VALIDATION_ERROR`` on ``details.fields[sort]``.
    Map the returned field names to columns yourself; never interpolate unchecked input."""
    raw = sort or default
    if not raw:
        return []
    result = []
    for part in raw.split(","):
        field, sep, direction = part.strip().partition(":")
        if not sep or field not in allowed or direction not in ("asc", "desc"):
            raise validation_error([{"field": "sort", "code": "invalid_sort"}])
        result.append((field, direction))
    return result


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int

    @classmethod
    def build(cls, items: list[T], *, total: int, params: PageParams) -> "Page[T]":
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)
