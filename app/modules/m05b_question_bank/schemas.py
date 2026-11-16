"""M5b shapes. Create/patch reuse M5's ``QuestionIn`` so a bank item always converts to a question body."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.m05_assessments.schemas import Num, OptionIn, QuestionIn, QuestionType, ScoringMode

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class BankItemCreate(QuestionIn):
    tags: list[Tag] = Field(default_factory=list, max_length=20)


class AsQuestionOut(QuestionIn):
    """``QuestionIn`` shape with ``marks`` as a JSON number (spec 4.6)."""

    marks: Num = 1


class BankItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: QuestionType | None = None
    prompt_html: str | None = Field(default=None, min_length=1, max_length=20000)
    marks: Num | None = None
    scoring_mode: ScoringMode | None = None
    options: list[OptionIn] | None = Field(default=None, max_length=10)
    answer_key: dict[str, Any] | None = None
    model_answer: str | None = Field(default=None, max_length=10000)
    code_language: str | None = Field(default=None, max_length=40)
    tags: list[Tag] | None = Field(default=None, max_length=20)


class BankItemOut(BaseModel):
    id: UUID
    owner_id: UUID
    type: QuestionType
    prompt_html: str
    marks: Num
    scoring_mode: ScoringMode
    options: list[OptionIn]
    answer_key: dict[str, Any] | list | None
    model_answer: str | None
    code_language: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
