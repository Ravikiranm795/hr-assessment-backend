"""M8 shapes. Answer payloads match ``m05_assessments.scoring_defs.grade_auto``:
single_choice ``{"option_id"}``, multi_choice ``{"option_ids": []}``, fill_blank ``{"blanks": []}``,
code_gap ``{"gaps": []}``, short_answer ``{"text"}``."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.m05_assessments.schemas import PreviewQuestion
from app.modules.m07_invites.schemas import AttemptSummary  # noqa: F401  (re-exported shape)

HelpReason = Literal["no_camera", "camera_not_working", "extra_time", "other"]


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: Annotated[str, StringConstraints(min_length=16, max_length=200)]


class SessionOut(BaseModel):
    session_token: str
    token_type: str = "Bearer"
    attempt_status: str
    expires_in_sec: int


class SystemCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    viewport_w: Annotated[int, Field(strict=True, ge=0, le=20000)]
    viewport_h: Annotated[int, Field(strict=True, ge=0, le=20000)]
    camera_permission: bool = False


class SystemCheckOut(BaseModel):
    device_ok: bool
    camera_required: bool
    status: str


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_version_id: UUID | None = None


class ConsentDocOut(BaseModel):
    document_version_id: UUID
    content_html: str | None
    pdf_url: str | None


class StateOut(BaseModel):
    status: str
    role_title: str | None
    duration_minutes: int
    instructions_html: str | None
    header: dict[str, Any]
    footer_html: str | None
    watermark: str | None
    branding: dict[str, Any]
    consent_document: ConsentDocOut | None
    consented: bool
    camera_required: bool
    baseline_photo_uploaded: bool
    help_request_pending: bool
    accommodations: dict[str, Any]
    snapshot_interval_seconds: int
    started_at: datetime | None
    deadline_at: datetime | None
    remaining_seconds: int | None
    question_count: int


class TimingOut(BaseModel):
    status: str
    started_at: datetime | None
    deadline_at: datetime | None
    remaining_seconds: int | None


class QuestionsOut(BaseModel):
    questions: list[PreviewQuestion]
    answers: dict[str, Any]
    remaining_seconds: int


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: Any


class AnswerOut(BaseModel):
    saved: bool = True
    remaining_seconds: int


class HeartbeatOut(BaseModel):
    status: str
    remaining_seconds: int
    time_over: bool


class EventIn(BaseModel):
    """``severity`` sent by a client is ignored (extra fields are dropped); the server assigns it."""

    model_config = ConfigDict(extra="ignore")
    type: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    occurred_at: datetime | None = None
    duration_ms: Annotated[int, Field(strict=True, ge=0, le=86_400_000)] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class EventsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: Annotated[list[EventIn], Field(min_length=1, max_length=50)]


class EventsOut(BaseModel):
    accepted: int
    ignored: int


class SnapshotOut(BaseModel):
    event_id: UUID


class HelpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: HelpReason
    note: Annotated[str, Field(max_length=500)] | None = None


class HelpOut(BaseModel):
    id: UUID
    reason: str
    note: str | None
    status: str
    created_at: datetime
    decided_at: datetime | None
    granted_camera_exempt: bool | None
    granted_extra_minutes: int | None


class HelpCurrentOut(BaseModel):
    request: HelpOut | None


class SubmitOut(BaseModel):
    status: str
    submitted_at: datetime | None
    submit_reason: str | None
