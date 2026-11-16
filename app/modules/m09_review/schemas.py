"""M9 shapes."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints

Num = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoreOverrideIn(_Strict):
    marks: Annotated[Decimal, Field(ge=0, le=1000, decimal_places=2)]
    reason: Reason


class DismissIn(_Strict):
    note: Note


class ExtraTimeIn(_Strict):
    minutes: Annotated[int, Field(strict=True, ge=1, le=240)]
    reason: Reason


class DecisionIn(_Strict):
    decision: Literal["approved", "rejected"]
    note: Annotated[str, Field(max_length=500)] | None = None


class OverrideIn(_Strict):
    decision: Literal["approved", "rejected", "waiting"]
    reason: Reason


class AttemptRow(BaseModel):
    id: UUID
    invite_id: UUID
    candidate_name: str
    candidate_email: str | None
    status: str
    scoring_status: str
    score_percent: Num | None
    passed: bool | None
    decision: str
    flag_counts: dict
    submitted_at: datetime | None


class AttemptRowLimited(BaseModel):
    """Non-owner HR: name, status and decision only (spec 4.3)."""

    id: UUID
    candidate_name: str
    status: str
    decision: str


class AnswerRow(BaseModel):
    question_id: UUID
    position: int
    type: str
    prompt_html: str
    marks: Num
    scoring_mode: str
    options: list[dict[str, Any]]
    answer_key: Any
    model_answer: str | None
    answer: Any
    auto_marks: Num | None
    ai_marks: Num | None
    ai_reason: str | None
    manual_marks: Num | None
    manual_reason: str | None
    final_marks: Num | None
    scored_by: str | None


class EventRow(BaseModel):
    id: UUID
    type: str
    severity: str
    occurred_at: datetime | None
    received_at: datetime
    duration_ms: int | None
    snapshot_asset_id: UUID | None
    dismissed: bool
    dismissed_note: str | None


class DecisionRow(BaseModel):
    id: UUID
    decision: str
    previous_decision: str | None
    reason: str | None
    is_override: bool
    decided_by: UUID
    created_at: datetime


class AttemptReport(BaseModel):
    id: UUID
    status: str
    scoring_status: str
    started_at: datetime | None
    deadline_at: datetime | None
    submitted_at: datetime | None
    submit_reason: str | None
    auto_score: Num | None
    ai_score: Num | None
    final_score: Num | None
    score_percent: Num | None
    passed: bool | None
    total_marks: Num
    pass_mark_percent: Num
    decision: str
    decision_note: str | None
    candidate: dict[str, Any]
    badges: dict[str, Any]
    consent: dict[str, Any]
    breakdown: list[AnswerRow]
    flag_counts: dict
    timeline: list[EventRow]
    snapshot_asset_ids: list[UUID]
    baseline_asset_id: UUID | None
    help_requests: list[dict[str, Any]]
    decision_history: list[DecisionRow]


class SnapshotUrlOut(BaseModel):
    url: str
    expires_in_sec: int = 60


class ScoringOut(BaseModel):
    attempt_id: UUID
    scoring_status: str
    score_percent: Num | None
    passed: bool | None


class ErasedOut(BaseModel):
    answers: int
    events: int
    snapshots: int
    identity_erased: bool
