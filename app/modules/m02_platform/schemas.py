"""M2 request and response models."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.branding_text import BACKGROUND_STYLES, FONT_KEYS
from app.modules.m03_team.schemas import Email, Locale, Name, Reason, UserOut

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", min_length=2, max_length=63
    ),
]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]

_ORG = {
    "id": "7d1b8f1e-3c5a-4e0b-8a52-9d6f0c2b1e44",
    "name": "Impelsys",
    "slug": "impelsys",
    "status": "active",
    "retention_days": 90,
    "locale": "en",
    "timezone": "UTC",
    "is_demo": False,
    "created_at": "2026-11-16T09:30:00Z",
    "updated_at": "2026-11-16T09:30:00Z",
}


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_schema_extra={"examples": [_ORG]})

    id: UUID
    name: str
    slug: str
    status: str
    retention_days: int
    locale: str
    timezone: str
    is_demo: bool
    created_at: datetime
    updated_at: datetime


class AdminIn(BaseModel):
    name: Name
    email: Email


class OrgCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"name": "Impelsys", "admin": {"name": "Asha Admin", "email": "asha@impelsys.example"}}
            ]
        }
    )

    name: OrgName
    slug: Slug | None = Field(None, description="Derived from the name when omitted.")
    locale: Locale | None = None
    timezone: str | None = Field(None, max_length=64)
    is_demo: bool = False
    admin: AdminIn = Field(description="The first organization admin.")


class OrgCreatedOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "organization": _ORG,
                    "admin": {"id": "0b6f5c0e-5d3a-4c8e-9a64-2f1c3e8d7a10", "role": "org_admin"},
                    "temporary_password": "q7Hn-Vb3x!Rt9LmZ",
                }
            ]
        }
    )

    organization: OrgOut
    admin: UserOut
    temporary_password: str = Field(description="Shown once, never stored in clear text.")


class OrgUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Impelsys Ltd", "is_demo": False}]})

    name: OrgName | None = None
    is_demo: bool | None = None


class StatusChangeIn(BaseModel):
    reason: Reason | None = None


class OverviewOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "users": {"total": 5, "active": 4, "inactive": 1, "org_admins": 1, "hr_users": 4},
                    "open_roles": 2,
                    "roles_total": 3,
                    "invites": {
                        "total": 10,
                        "invited": 4,
                        "started": 1,
                        "submitted": 5,
                        "expired": 0,
                        "revoked": 0,
                    },
                    "attempts": {"total": 6, "submitted": 5},
                    "ai_usage": {"calls_this_month": 12},
                }
            ]
        }
    )

    users: dict[str, int]
    open_roles: int
    roles_total: int
    invites: dict[str, int]
    attempts: dict[str, int]
    ai_usage: dict[str, int]


class OrgRoleOut(BaseModel):
    id: UUID
    title: str
    department: str | None
    status: str
    owner_id: UUID
    owner_name: str
    owner_inactive: bool
    created_at: datetime


class PlatformDeactivateIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"confirm_last_admin": True, "reason": "Contract ended"}]}
    )

    confirm_last_admin: bool = Field(
        False, description="Required to deactivate the last active admin of an organization."
    )
    reassign_roles_to: UUID | None = None
    reason: Reason | None = None


class AdminTempPasswordOut(BaseModel):
    user_id: UUID
    temporary_password: str = Field(description="Shown once. The admin must change it at first login.")
    must_change_password: bool = True


class FlagCatalogItem(BaseModel):
    key: str
    description: str
    default_enabled: bool
    visible_to_org: bool
    depends_on: list[str]


class FlagCatalogOut(BaseModel):
    items: list[FlagCatalogItem]


class OrgFlagState(BaseModel):
    enabled: bool = Field(description="The stored value (override or default).")
    effective: bool = Field(
        description="After dependencies (for example invites needs roles and assessments)."
    )
    visible_to_org: bool


class OrgFlagsOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "org_id": _ORG["id"],
                    "flags": {"team": {"enabled": True, "effective": True, "visible_to_org": True}},
                }
            ]
        }
    )

    org_id: UUID
    flags: dict[str, OrgFlagState]


class FlagsIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"flags": {"reports_chat": False}, "reason": "Not in contract"}]}
    )

    flags: dict[str, bool] = Field(
        min_length=1, description="Flag key to enabled. Keys must be in the catalog."
    )
    reason: Reason | None = None


class DefaultsIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"defaults": {"audit_export": False}}]})

    defaults: dict[str, bool] = Field(min_length=1)
    reason: Reason | None = None


class DefaultsOut(BaseModel):
    defaults: dict[str, bool]


class MisuseFlagCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"comment": "Candidate links were posted publicly", "user_id": None}]}
    )

    comment: Comment = Field(description="Mandatory. The organization admin sees it in a notification.")
    user_id: UUID | None = Field(None, description="Optional user of that organization the flag is about.")


class MisuseFlagClear(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"closing_note": "Resolved by email on 17 Nov"}]}
    )

    closing_note: Comment


class MisuseFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    user_id: UUID | None
    raised_by: UUID
    comment: str
    status: str
    raised_at: datetime
    cleared_by: UUID | None
    closing_note: str | None
    cleared_at: datetime | None


class BrandingIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "primary_color": "#1B2A4A",
                    "accent_color": "#F26B5B",
                    "background_style": "mint",
                    "font_key": "inter",
                }
            ]
        }
    )

    primary_color: HexColor | None = None
    accent_color: HexColor | None = None
    background_style: Literal[BACKGROUND_STYLES] | None = None  # type: ignore[valid-type]
    font_key: Literal[FONT_KEYS] | None = None  # type: ignore[valid-type]


class BrandingOut(BaseModel):
    scope: str = "platform"
    primary_color: str
    accent_color: str
    background_style: str
    font_key: str
    is_default: bool = Field(
        description="True while the platform theme was never customized (starter theme)."
    )
    updated_at: datetime | None


class UsageItem(BaseModel):
    org_id: UUID
    org_name: str
    calls: int
    tokens_in: int
    tokens_out: int
    by_feature: dict[str, int]


class UsageOut(BaseModel):
    month: str
    items: list[UsageItem]
    total: int
    page: int
    page_size: int
