"""FilterSpec (spec 7.4 step 2-3): the structured query the AI proposes and the backend validates."""

from datetime import UTC, date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import AppError
from app.core.security import blind_index
from app.modules.m11_reports.registry import FieldDef, Source, sources_for

MAX_LIMIT = 50_000
Op = Literal["eq", "in", "contains", "between", "gte", "lte"]


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    op: Op
    value: Any


class Sort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    dir: Literal["asc", "desc"] = "asc"


class Aggregate(BaseModel):
    """Phase one: count, grouped by one field (month buckets for datetimes)."""

    model_config = ConfigDict(extra="forbid")
    group_by: str
    bucket: Literal["month"] | None = None
    metric: Literal["count"] = "count"


class FilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    columns: list[str] = Field(default_factory=list, max_length=30)
    sort: list[Sort] = Field(default_factory=list, max_length=3)
    aggregate: Aggregate | None = None
    limit: int = Field(default=MAX_LIMIT, ge=1, le=MAX_LIMIT)


def invalid(field: str, code: str, **extra: Any) -> AppError:
    return AppError("REPORT_FILTER_INVALID", details={"fields": [{"field": field, "code": code, **extra}]})


def _date_or_datetime(value: Any, *, end_of_day: bool, where: str) -> datetime:
    if not isinstance(value, str):
        raise invalid(where, "wrong_type")
    try:
        parsed = datetime.fromisoformat(value) if "T" in value or " " in value else date.fromisoformat(value)
    except ValueError:
        raise invalid(where, "invalid_datetime") from None
    if isinstance(parsed, date) and not isinstance(parsed, datetime):
        parsed = datetime.combine(parsed, time.max if end_of_day else time.min)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _scalar(f: FieldDef, value: Any, where: str, *, end_of_day: bool = False) -> Any:
    if f.type == "datetime":
        return _date_or_datetime(value, end_of_day=end_of_day, where=where)
    if f.type == "bool":
        if not isinstance(value, bool):
            raise invalid(where, "wrong_type")
        return value
    if f.type in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
            raise invalid(where, "wrong_type")
        if f.type == "int":
            if not float(value).is_integer():
                raise invalid(where, "wrong_type")
            return int(value)
        return value
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise invalid(where, "wrong_type")
    if f.type == "enum" and value not in (f.values or ()):
        raise invalid(where, "value_not_allowed", allowed=list(f.values or ()))
    return value.strip()


def coerce(f: FieldDef, flt: Filter, index: int) -> Any:
    """The typed bind value for a filter, or ``REPORT_FILTER_INVALID``."""
    where = f"filters.{index}.value"
    if flt.op not in f.ops:
        raise invalid(f"filters.{index}.op", "op_not_allowed", allowed=list(f.ops))
    if flt.op == "in":
        if not isinstance(flt.value, list) or not 1 <= len(flt.value) <= 20:
            raise invalid(where, "wrong_type")
        return [_scalar(f, v, where) for v in flt.value]
    if flt.op == "between":
        if not isinstance(flt.value, list) or len(flt.value) != 2:
            raise invalid(where, "wrong_type")
        return [_scalar(f, flt.value[0], where), _scalar(f, flt.value[1], where, end_of_day=True)]
    if flt.op == "lte":
        return _scalar(f, flt.value, where, end_of_day=True)
    value = _scalar(f, flt.value, where)
    return blind_index(value) if f.name == "candidate_email" and flt.op == "eq" else value


def validate_spec(raw: dict | FilterSpec, *, platform: bool = False) -> FilterSpec:
    """Raises ``REPORT_FILTER_INVALID``: unknown source, field, operator or value, or a column the
    scope may not use. Fills default columns."""
    try:
        spec = raw if isinstance(raw, FilterSpec) else FilterSpec.model_validate(raw)
    except ValidationError as exc:
        fields = [{"field": ".".join(str(p) for p in e["loc"]), "code": e["type"]} for e in exc.errors()]
        raise AppError("REPORT_FILTER_INVALID", details={"fields": fields}) from None
    source = sources_for(platform).get(spec.source)
    if source is None:
        raise invalid("source", "unknown_source", allowed=sorted(sources_for(platform)))
    for i, flt in enumerate(spec.filters):
        f = _field(source, flt.field, f"filters.{i}.field")
        coerce(f, flt, i)
    for i, name in enumerate(spec.columns):
        _field(source, name, f"columns.{i}")
    for i, s in enumerate(spec.sort):
        if not _field(source, s.field, f"sort.{i}.field").sortable:
            raise invalid(f"sort.{i}.field", "not_sortable")
    if spec.aggregate:
        g = _field(source, spec.aggregate.group_by, "aggregate.group_by")
        if not g.groupable:
            raise invalid("aggregate.group_by", "not_groupable")
        if spec.aggregate.bucket and g.type != "datetime":
            raise invalid("aggregate.bucket", "only_for_datetime")
        if g.pii:
            raise invalid("aggregate.group_by", "pii_not_groupable")
    if not spec.columns and not spec.aggregate:
        spec = spec.model_copy(update={"columns": list(source.default_columns)})
    return spec


def _field(source: Source, name: str, where: str) -> FieldDef:
    f = source.fields.get(name)
    if f is None:
        raise invalid(where, "unknown_field")
    return f
