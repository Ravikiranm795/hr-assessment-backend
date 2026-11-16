"""Request and response shapes for M5. ``QuestionIn`` is also the body M5b's ``as-question`` returns."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints, field_validator

QuestionType = Literal["single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer"]
ScoringMode = Literal["auto", "ai", "manual"]
AssessmentStatus = Literal["active", "archived"]

# Numbers are raw on the wire (spec 4.6): Decimal columns are serialized as JSON numbers.
Num = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- questions


class OptionIn(_Strict):
    text_html: str = Field(min_length=1, max_length=5000)
    is_correct: bool = False


class QuestionIn(_Strict):
    """Create-question body. ``answer_key`` shape by type:

    - ``multi_choice``: ``{"partial": bool}`` (partial credit, default all-or-nothing)
    - ``fill_blank``: ``{"blanks": [{"accepted": ["..."]}], "case_sensitive": false}``
    - ``code_gap``: ``{"gaps": [{"accepted": ["..."]}], "case_sensitive": true, "key_points": ["..."]}``
    - ``short_answer``: ``{"key_points": ["..."]}``
    """

    type: QuestionType
    prompt_html: str = Field(min_length=1, max_length=20000)
    marks: Annotated[Decimal, Field(ge=0, le=1000, decimal_places=2)] = Decimal("1")
    scoring_mode: ScoringMode | None = Field(
        default=None, description="Defaults: auto for choice, blanks and gaps; manual for short answer."
    )
    options: list[OptionIn] = Field(default_factory=list, max_length=10)
    answer_key: dict[str, Any] | None = None
    model_answer: str | None = Field(default=None, max_length=10000)
    code_language: str | None = Field(default=None, max_length=40)

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "type": "single_choice",
                "prompt_html": "<p>What is 2 + 2?</p>",
                "marks": 1,
                "options": [{"text_html": "4", "is_correct": True}, {"text_html": "5", "is_correct": False}],
            }
        },
    )


class QuestionPatch(_Strict):
    type: QuestionType | None = None
    prompt_html: str | None = Field(default=None, min_length=1, max_length=20000)
    marks: Annotated[Decimal, Field(ge=0, le=1000, decimal_places=2)] | None = None
    scoring_mode: ScoringMode | None = None
    options: list[OptionIn] | None = Field(default=None, max_length=10)
    answer_key: dict[str, Any] | None = None
    model_answer: str | None = Field(default=None, max_length=10000)
    code_language: str | None = Field(default=None, max_length=40)


class BulkQuestionsIn(_Strict):
    questions: list[QuestionIn] = Field(min_length=1, max_length=50)


class ReorderIn(_Strict):
    question_ids: list[UUID] = Field(min_length=1, max_length=500)


class OptionOut(BaseModel):
    id: UUID
    position: int
    text_html: str
    is_correct: bool


class QuestionOut(BaseModel):
    id: UUID
    position: int
    type: QuestionType
    prompt_html: str
    marks: Num
    scoring_mode: ScoringMode
    options: list[OptionOut]
    answer_key: dict[str, Any] | list | None
    model_answer: str | None
    code_language: str | None


class QuestionList(BaseModel):
    items: list[QuestionOut]


# --------------------------------------------------------------------------- versions


class HeaderConfig(_Strict):
    company_name: str | None = Field(default=None, max_length=200)
    logo_asset_id: UUID | None = None


class ProctoringConfig(_Strict):
    snapshot_interval_seconds: int = Field(default=60, ge=10, le=600)


class VersionPatch(_Strict):
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    pass_mark_percent: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)] | None = None
    instructions_html: str | None = Field(default=None, max_length=20000)
    header_config: HeaderConfig | None = None
    footer_html: str | None = Field(default=None, max_length=20000)
    proctoring_config: ProctoringConfig | None = None
    shuffle_questions: bool | None = None
    shuffle_options: bool | None = None

    @field_validator("duration_minutes", "pass_mark_percent", mode="before")
    @classmethod
    def _no_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("null not allowed")
        return value


class VersionSummary(BaseModel):
    id: UUID
    version_no: int
    status: Literal["draft", "published"]
    duration_minutes: int
    pass_mark_percent: Num
    total_marks: Num
    question_count: int
    in_use: bool = Field(
        description="Protected from purge: open role link, undecided attempt or pending invite."
    )
    published_at: datetime | None
    created_at: datetime


class VersionOut(VersionSummary):
    assessment_id: UUID
    instructions_html: str | None
    header_config: dict[str, Any]
    footer_html: str | None
    proctoring_config: dict[str, Any]
    shuffle_questions: bool
    shuffle_options: bool
    questions: list[QuestionOut]


class VersionList(BaseModel):
    items: list[VersionSummary]


# --------------------------------------------------------------------------- assessments


class AssessmentCreate(_Strict):
    title: Title
    description: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"title": "Python basics", "description": "Screening"}}
    )


class AssessmentPatch(_Strict):
    title: Title | None = None
    description: str | None = Field(default=None, max_length=2000)


class AssessmentListItem(BaseModel):
    id: UUID
    title: str
    description: str | None
    owner_id: UUID
    status: AssessmentStatus
    version_count: int
    latest_version_no: int | None
    latest_published_version_id: UUID | None
    has_draft: bool
    created_at: datetime
    updated_at: datetime


class AssessmentDetail(AssessmentListItem):
    versions: list[VersionSummary]


# --------------------------------------------------------------------------- preview


class PreviewOption(BaseModel):
    id: UUID
    text_html: str


class PreviewQuestion(BaseModel):
    id: UUID
    position: int
    type: QuestionType
    prompt_html: str
    marks: Num
    options: list[PreviewOption]
    blank_count: int | None
    code_language: str | None


class PreviewHeader(BaseModel):
    company_name: str | None
    logo_asset_id: UUID | None


class PreviewOut(BaseModel):
    """What a candidate sees: no answer keys, model answers, scoring modes or correct flags."""

    version_id: UUID
    version_no: int
    status: Literal["draft", "published"]
    header: PreviewHeader
    instructions_html: str | None
    footer_html: str | None
    watermark: str | None = Field(description="Watermark text, or null when the SA turned the flag off.")
    duration_minutes: int
    shuffle_questions: bool
    shuffle_options: bool
    total_marks: Num
    questions: list[PreviewQuestion]
