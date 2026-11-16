"""Request and response models for the audit APIs (spec 6/M10)."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core import audit
from app.core.pagination import Page

ActorType = Literal["user", "candidate", "system"]
ResultValue = Literal["success", "failure", "denied"]
ExportFormat = Literal["csv", "pdf"]


class AuditFilters(BaseModel):
    """Manual filters of the audit screen. Without ``date_from`` the window is the 30 days
    before ``date_to`` (or now), so queries stay inside a few monthly partitions."""

    model_config = ConfigDict(extra="forbid")

    actor_user_id: UUID | None = Field(None, description="Acting user")
    actor_type: ActorType | None = None
    action: list[str] | None = Field(None, description="One or more action names (Appendix B)")
    entity_type: str | None = Field(None, max_length=100)
    entity_id: str | None = Field(None, max_length=200)
    result: ResultValue | None = None
    date_from: datetime | None = Field(None, description="Inclusive, ISO 8601 (naive means UTC)")
    date_to: datetime | None = Field(None, description="Inclusive, ISO 8601 (naive means UTC)")
    q: str | None = Field(
        None, max_length=200, description="Text search over actor, action, entity and reason"
    )

    @field_validator("date_from", "date_to")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @field_validator("action")
    @classmethod
    def _known_actions(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(a not in audit.AUDIT_ACTIONS for a in value):
            raise ValueError("unknown_action")
        return value or None

    @model_validator(mode="after")
    def _ordered(self) -> "AuditFilters":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_range_invalid")
        return self


class PlatformAuditFilters(AuditFilters):
    org_id: UUID | None = Field(None, description="Restrict to one organization (super admin view)")


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: ExportFormat = Field(description="`csv` or `pdf`")
    columns: list[str] | None = Field(
        None, description="Column keys from GET /audit-logs/columns (default set if empty)"
    )
    filters: AuditFilters = Field(default_factory=AuditFilters)


class PlatformExportRequest(ExportRequest):
    filters: PlatformAuditFilters = Field(default_factory=PlatformAuditFilters)  # type: ignore[assignment]


class AuditLogItem(BaseModel):
    id: UUID
    created_at: datetime
    org_id: UUID | None
    actor_type: str
    actor_user_id: UUID | None
    actor_name: str | None
    actor_role: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    result: str
    reason: str | None
    ip: str | None
    request_id: str | None


class AuditLogDetail(AuditLogItem):
    user_agent: str | None
    before: dict[str, Any] | None = Field(description="Redacted values only")
    after: dict[str, Any] | None = Field(description="Redacted values only")


class AuditPage(Page[AuditLogItem]):
    date_from: datetime = Field(description="Effective lower bound of the date window")
    date_to: datetime | None = Field(description="Effective upper bound, null when open")


class SecurityEventsPage(AuditPage):
    failed_logins: int = Field(description="Failed login count in the window")
    lockouts: int = Field(description="Account lockout count in the window")


class ActionItem(BaseModel):
    action: str
    label_key: str


class ActionGroup(BaseModel):
    key: str
    label_key: str
    actions: list[ActionItem]


class ActionCatalog(BaseModel):
    groups: list[ActionGroup]


class ColumnItem(BaseModel):
    key: str
    label_key: str
    default: bool


class ColumnCatalog(BaseModel):
    items: list[ColumnItem]
