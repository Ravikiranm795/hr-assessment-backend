"""Compile a validated FilterSpec to a parameterized query (spec 7.4 step 4).

Only catalog expressions are ever put in the SQL text; every user value is a bind parameter, and ``org_id``
is forced into the WHERE of every org-scoped source."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.modules.m11_reports.filterspec import FilterSpec, coerce, validate_spec
from app.modules.m11_reports.registry import SOURCES, FieldDef, Source


@dataclass(frozen=True)
class Compiled:
    sql: str
    count_sql: str
    params: dict[str, Any]
    columns: list[str]
    decrypt_columns: tuple[str, ...]


def _predicate(f: FieldDef, op: str, name: str, value: Any, params: dict[str, Any]) -> str:
    if op == "between":
        params[f"{name}a"], params[f"{name}b"] = value
        return f"{f.expr} BETWEEN :{name}a AND :{name}b"
    if op == "in":
        params[name] = value
        return f"{f.expr} = ANY(:{name})"
    if op == "contains":
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params[name] = f"%{escaped}%"
        return f"{f.expr} ILIKE :{name} ESCAPE '\\'"
    params[name] = value
    if f.name == "candidate_email":  # compared by blind index, never by decrypting
        return f"i.candidate_email_hash = :{name}"
    return {"eq": f"{f.expr} = :{name}", "gte": f"{f.expr} >= :{name}", "lte": f"{f.expr} <= :{name}"}[op]


def compile_spec(
    spec: FilterSpec, org_id: UUID | None, *, row_cap: int | None = None, platform: bool = False
) -> Compiled:
    spec = validate_spec(spec, platform=platform)
    source: Source = SOURCES[spec.source]
    params: dict[str, Any] = {"org_id": org_id} if source.org_scoped else {}
    where = []
    for i, flt in enumerate(spec.filters):
        f = source.fields[flt.field]
        where.append(_predicate(f, flt.op, f"p{i}", coerce(f, flt, i), params))
    tail = "".join(f" AND {w}" for w in where)
    body = f"{source.from_sql}{tail}"

    if spec.aggregate:
        g = source.fields[spec.aggregate.group_by]
        group_expr = f"date_trunc('month', {g.expr})" if spec.aggregate.bucket else g.expr
        sql = f"SELECT {group_expr} AS {g.name}, count(*) AS count {body} GROUP BY 1 ORDER BY 1"
        count_sql = f"SELECT count(*) AS n FROM (SELECT 1 {body} GROUP BY {group_expr}) g"
        return Compiled(sql + _limit(spec, row_cap, params), count_sql, params, [g.name, "count"], ())

    select = ", ".join(f"{source.fields[c].expr} AS {c}" for c in spec.columns)
    order = ", ".join(f"{source.fields[s.field].expr} {s.dir.upper()}" for s in spec.sort)
    sql = f"SELECT {select} {body}" + (f" ORDER BY {order}" if order else "")
    count_sql = f"SELECT count(*) AS n {body}"
    decrypt = tuple(c for c in spec.columns if c == "candidate_email")
    return Compiled(sql + _limit(spec, row_cap, params), count_sql, params, list(spec.columns), decrypt)


def _limit(spec: FilterSpec, row_cap: int | None, params: dict[str, Any]) -> str:
    """``row_cap + 1`` rows are fetched so the caller can tell the cap was exceeded."""
    params["row_limit"] = min(spec.limit, row_cap + 1) if row_cap else spec.limit
    return " LIMIT :row_limit"
