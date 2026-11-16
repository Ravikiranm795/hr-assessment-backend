"""M7 request and response shapes."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, StringConstraints

Email = Annotated[EmailStr, BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 ()\-]{5,25}$")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
Num = Annotated[Decimal, Field(ge=Decimal("0.5"), le=Decimal("4"), decimal_places=2)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccommodationsIn(_Strict):
    camera_exempt: bool | None = None
    time_multiplier: Num | None = None
    extra_minutes: Annotated[int, Field(strict=True, ge=0, le=600)] | None = None
    reason: Reason


class InviteCreate(_Strict):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"candidate_name": "Asha Rao", "email": "asha@example.com", "expires_in_hours": 72}
        },
    )
    candidate_name: Name
    email: Email
    phone: Phone | None = None
    expires_in_hours: Annotated[int, Field(strict=True, ge=1, le=720)] | None = Field(
        None, description="Defaults to the organization's invite expiry setting."
    )
    allow_duplicate: bool = False
    send_email: bool = Field(False, description="Email the link now (SMTP must be verified).")
    accommodations: AccommodationsIn | None = None


class ExtendIn(_Strict):
    expires_at: datetime
    reason: Reason


class ReasonIn(_Strict):
    reason: Reason


class AttemptSummary(BaseModel):
    id: UUID
    status: str
    score_percent: float | None
    passed: bool | None
    decision: str
    flag_counts: dict
    submitted_at: datetime | None


class InviteDetail(BaseModel):
    id: UUID
    role_id: UUID
    assessment_version_id: UUID | None
    candidate_name: str
    candidate_email: str | None
    candidate_phone: str | None
    status: str
    expires_at: datetime
    camera_exempt: bool
    time_multiplier: float
    extra_minutes: int
    accommodation_reason: str | None
    duplicate_override: bool
    session_bound: bool
    created_at: datetime
    attempt: AttemptSummary | None


class InviteCreated(BaseModel):
    invite: InviteDetail
    exam_url: str = Field(description="Shown once. Only a hash is stored.")
    email_sent: bool = False


class LinkOut(BaseModel):
    invite_id: UUID
    exam_url: str


class EmailSentOut(BaseModel):
    invite_id: UUID
    sent: bool = True


class InviteRow(BaseModel):
    id: UUID
    candidate_name: str
    candidate_email: str | None
    status: str
    expires_at: datetime
    created_at: datetime
    assessment_version_id: UUID | None
    accommodations: bool
    attempt: AttemptSummary | None


class InviteRowLimited(BaseModel):
    """Non-owner HR: name, status and decision only (spec 4.3)."""

    id: UUID
    candidate_name: str
    status: str
    decision: str | None


class BulkRowResult(BaseModel):
    row: int
    status: str
    code: str | None = None
    invite_id: UUID | None = None
    exam_url: str | None = None


class BulkResult(BaseModel):
    total: int
    created: int
    failed: int
    rows: list[BulkRowResult]


class HelpRequestOut(BaseModel):
    id: UUID
    invite_id: UUID
    role_id: UUID
    role_title: str
    candidate_name: str
    reason: str
    note: str | None
    status: str
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None
    granted_camera_exempt: bool | None
    granted_extra_minutes: int | None


class ApproveIn(_Strict):
    camera_exempt: bool = False
    extra_minutes: Annotated[int, Field(strict=True, ge=0, le=600)] = 0
    note: Annotated[str, Field(max_length=500)] | None = None


class DeclineIn(_Strict):
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
