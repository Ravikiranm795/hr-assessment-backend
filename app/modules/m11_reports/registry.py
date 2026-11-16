"""Field catalog (spec 7.4 step 1, Appendix F): the only things a report can select, filter or group by."""

# ruff: noqa: E501  # (SQL expressions in the catalog)

from dataclasses import dataclass

from app.db import enums


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str  # text | int | number | bool | datetime | enum
    ops: tuple[str, ...]
    expr: str
    pii: bool = False
    values: tuple[str, ...] | None = None
    sortable: bool = True
    groupable: bool = False

    @property
    def label_key(self) -> str:
        return f"reports.fields.{self.name}"


@dataclass(frozen=True)
class Source:
    name: str
    from_sql: str  # FROM ... WHERE <scope>, org sources use :org_id
    fields: dict[str, FieldDef]
    default_columns: tuple[str, ...]
    platform: bool = (
        False  # super admin sources: organization-level aggregates and audit, no candidate content
    )
    org_scoped: bool = True


TEXT, EQ_TEXT = ("eq", "contains"), ("eq",)
DT = ("between", "gte", "lte")
NUM = ("gte", "lte", "between")
ENUM = ("eq", "in")


def _fields(*defs: FieldDef) -> dict[str, FieldDef]:
    return {d.name: d for d in defs}


_CANDIDATES = Source(
    name="candidates",
    from_sql=(
        "FROM invites i JOIN open_roles r ON r.id = i.role_id AND r.deleted_at IS NULL "
        "LEFT JOIN attempts a ON a.invite_id = i.id "
        "LEFT JOIN assessment_versions v ON v.id = i.assessment_version_id "
        "LEFT JOIN assessments s ON s.id = v.assessment_id "
        "LEFT JOIN users ob ON ob.id = i.created_by LEFT JOIN users dby ON dby.id = a.decided_by "
        "WHERE i.org_id = :org_id"
    ),
    fields=_fields(
        FieldDef("candidate_name", "text", TEXT, "i.candidate_name", pii=True),
        FieldDef("candidate_email", "text", EQ_TEXT, "i.candidate_email_enc", pii=True, sortable=False),
        FieldDef("role_title", "text", TEXT, "r.title", groupable=True),
        FieldDef("department", "text", EQ_TEXT, "r.department", groupable=True),
        FieldDef("assessment_title", "text", TEXT, "s.title"),
        FieldDef("assessment_version", "int", ("eq",), "v.version_no"),
        FieldDef("invited_by_name", "text", EQ_TEXT, "ob.name", groupable=True),
        FieldDef("invite_status", "enum", ENUM, "i.status", values=enums.INVITE_STATUSES, groupable=True),
        FieldDef("invited_at", "datetime", DT, "i.created_at", groupable=True),
        FieldDef("started_at", "datetime", DT, "a.started_at"),
        FieldDef("submitted_at", "datetime", DT, "a.submitted_at", groupable=True),
        FieldDef("decided_at", "datetime", DT, "a.decided_at"),
        FieldDef("score_percent", "number", NUM, "a.score_percent"),
        FieldDef("passed", "bool", ("eq",), "a.passed"),
        FieldDef("decision", "enum", ENUM, "a.decision", values=enums.DECISIONS, groupable=True),
        FieldDef("decided_by_name", "text", EQ_TEXT, "dby.name"),
        FieldDef(
            "flag_counts_total", "int", ("gte",),
            "coalesce((SELECT sum(value::int) FROM jsonb_each_text(a.flag_counts->'types')), 0)",
        ),
    ),
    default_columns=("candidate_name", "candidate_email", "role_title", "invite_status", "score_percent", "decision"),
)  # fmt: skip

_ROLES = Source(
    name="roles",
    from_sql="FROM open_roles r LEFT JOIN users ow ON ow.id = r.owner_id WHERE r.org_id = :org_id AND r.deleted_at IS NULL",
    fields=_fields(
        FieldDef("title", "text", TEXT, "r.title"),
        FieldDef("department", "text", EQ_TEXT, "r.department", groupable=True),
        FieldDef("experience_level", "text", EQ_TEXT, "r.experience_level", groupable=True),
        FieldDef("vacancies", "int", NUM, "r.vacancies"),
        FieldDef("status", "enum", ENUM, "r.status", values=enums.ROLE_STATUSES, groupable=True),
        FieldDef("owner_name", "text", EQ_TEXT, "ow.name", groupable=True),
        FieldDef("created_at", "datetime", DT, "r.created_at", groupable=True),
        FieldDef("closed_at", "datetime", DT, "r.closed_at"),
        FieldDef("invites_total", "int", NUM, "(SELECT count(*) FROM invites x WHERE x.role_id = r.id)"),
        FieldDef("submitted_total", "int", NUM, "(SELECT count(*) FROM invites x WHERE x.role_id = r.id AND x.status = 'submitted')"),
    ),
    default_columns=("title", "department", "status", "owner_name", "invites_total"),
)  # fmt: skip

_ASSESSMENTS = Source(
    name="assessments",
    from_sql="FROM assessments s LEFT JOIN users ow ON ow.id = s.owner_id WHERE s.org_id = :org_id AND s.deleted_at IS NULL",
    fields=_fields(
        FieldDef("title", "text", TEXT, "s.title"),
        FieldDef("status", "enum", ENUM, "s.status", values=("active", "archived"), groupable=True),
        FieldDef("owner_name", "text", EQ_TEXT, "ow.name", groupable=True),
        FieldDef("created_at", "datetime", DT, "s.created_at", groupable=True),
        FieldDef("versions_total", "int", NUM, "(SELECT count(*) FROM assessment_versions x WHERE x.assessment_id = s.id)"),
        FieldDef("published_versions", "int", NUM, "(SELECT count(*) FROM assessment_versions x WHERE x.assessment_id = s.id AND x.status = 'published')"),
    ),
    default_columns=("title", "status", "owner_name", "versions_total"),
)  # fmt: skip

_TEAM = Source(
    name="team",
    from_sql="FROM users u WHERE u.org_id = :org_id AND u.role IN ('org_admin', 'hr_user')",
    fields=_fields(
        FieldDef("name", "text", TEXT, "u.name", pii=True),
        FieldDef("email", "text", EQ_TEXT, "u.email", pii=True),
        FieldDef("role", "enum", ENUM, "u.role", values=("org_admin", "hr_user"), groupable=True),
        FieldDef("status", "enum", ENUM, "u.status", values=enums.ACTIVE_STATUSES, groupable=True),
        FieldDef("last_login_at", "datetime", DT, "u.last_login_at"),
        FieldDef("created_at", "datetime", DT, "u.created_at", groupable=True),
    ),
    default_columns=("name", "email", "role", "status", "last_login_at"),
)

_AUDIT = Source(
    name="audit",
    from_sql="FROM audit_logs l WHERE l.org_id = :org_id",
    fields=_fields(
        FieldDef("action", "text", TEXT, "l.action", groupable=True),
        FieldDef("entity_type", "text", EQ_TEXT, "l.entity_type", groupable=True),
        FieldDef("actor", "text", TEXT, "l.actor_name_snapshot", groupable=True),
        FieldDef("result", "enum", ENUM, "l.result", values=("success", "failure", "denied"), groupable=True),
        FieldDef("created_at", "datetime", DT, "l.created_at", groupable=True),
    ),
    default_columns=("created_at", "action", "entity_type", "actor", "result"),
)

_ORGS = Source(
    name="organizations",
    from_sql="FROM organizations o WHERE true",
    fields=_fields(
        FieldDef("name", "text", TEXT, "o.name"),
        FieldDef("status", "enum", ENUM, "o.status", values=enums.ORG_STATUSES, groupable=True),
        FieldDef("created_at", "datetime", DT, "o.created_at", groupable=True),
        FieldDef("active_users", "int", NUM, "(SELECT count(*) FROM users u WHERE u.org_id = o.id AND u.status = 'active')"),
        FieldDef("open_roles", "int", NUM, "(SELECT count(*) FROM open_roles r WHERE r.org_id = o.id AND r.status = 'open' AND r.deleted_at IS NULL)"),
    ),
    default_columns=("name", "status", "active_users", "open_roles"),
    platform=True,
    org_scoped=False,
)  # fmt: skip

_PLATFORM_AUDIT = Source(
    name="platform_audit",
    from_sql="FROM audit_logs l LEFT JOIN organizations o ON o.id = l.org_id WHERE true",
    fields=_fields(
        FieldDef("action", "text", TEXT, "l.action", groupable=True),
        FieldDef("entity_type", "text", EQ_TEXT, "l.entity_type", groupable=True),
        FieldDef("org_name", "text", EQ_TEXT, "o.name", groupable=True),
        FieldDef("result", "enum", ENUM, "l.result", values=("success", "failure", "denied"), groupable=True),
        FieldDef("created_at", "datetime", DT, "l.created_at", groupable=True),
    ),
    default_columns=("created_at", "action", "org_name", "result"),
    platform=True,
    org_scoped=False,
)

SOURCES: dict[str, Source] = {
    s.name: s for s in (_CANDIDATES, _ROLES, _ASSESSMENTS, _TEAM, _AUDIT, _ORGS, _PLATFORM_AUDIT)
}
FIELD_CATALOG: dict[str, list[FieldDef]] = {name: list(s.fields.values()) for name, s in SOURCES.items()}


def sources_for(platform: bool) -> dict[str, Source]:
    return {n: s for n, s in SOURCES.items() if s.platform == platform}


def catalog_json(platform: bool) -> list[dict]:
    """Exactly what the AI is given and what drives the UI: metadata only, never a row."""
    return [
        {
            "source": s.name,
            "default_columns": list(s.default_columns),
            "fields": [
                {
                    "name": f.name, "type": f.type, "label_key": f.label_key, "ops": list(f.ops), "pii": f.pii,
                    "sortable": f.sortable, "groupable": f.groupable, **({"values": list(f.values)} if f.values else {}),
                }
                for f in s.fields.values()
            ],
        }
        for s in sources_for(platform).values()
    ]  # fmt: skip
