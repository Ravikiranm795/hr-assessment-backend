"""M11 request and response models (spec 6/M11)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TEXT = 2000


class FieldOut(BaseModel):
    name: str
    type: str
    label_key: str
    ops: list[str]
    pii: bool
    sortable: bool
    groupable: bool
    values: list[str] | None = None


class SourceOut(BaseModel):
    source: str
    default_columns: list[str]
    fields: list[FieldOut]


class CatalogOut(BaseModel):
    """Exactly what the AI is given: names, types, operators and allowed values; never rows."""

    sources: list[SourceOut]


class InterpretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT, description="The question")
    source: str | None = Field(default=None, max_length=40, description="Optional: limit to one source")
    filter_spec: dict | None = Field(
        default=None, description="A structured FilterSpec to validate and use without the AI"
    )

    @model_validator(mode="after")
    def _need_something(self) -> "InterpretIn":
        if not (self.text and self.text.strip()) and not self.filter_spec:
            raise ValueError("text_or_filter_spec_required")
        return self


class SelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(description="The report request returned by interpret")
    index: int | None = Field(default=None, ge=0, le=3, description="Interpretation to confirm")
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT, description="Refine instead")

    @model_validator(mode="after")
    def _one_of(self) -> "SelectIn":
        if (self.index is None) == (self.text is None):
            raise ValueError("index_or_text_required")
        return self


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["csv", "pdf"]
    columns: list[str] | None = Field(default=None, max_length=30, description="Replaces the columns")


class InterpretationOut(BaseModel):
    index: int
    label: str
    filter_spec: dict
    columns: list[str]
    estimated_rows: int | None = Field(description="Counted in the database, never by the AI")


class ReportOut(BaseModel):
    id: UUID
    source: str
    user_text: str | None
    status: Literal["interpreted", "confirmed", "generated", "failed"]
    filter_spec: dict | None = Field(description="The confirmed structured filter (null until confirmed)")
    columns: list[str] | None
    format: Literal["csv", "pdf"] | None
    row_count: int | None
    file_available: bool
    created_by: UUID
    created_at: datetime


class ChatOut(BaseModel):
    report: ReportOut
    interpretations: list[InterpretationOut]
    clarifying_question: str | None = None
    estimated_rows: int | None = Field(default=None, description="Set once an interpretation is confirmed")


class CountOut(BaseModel):
    id: UUID
    row_count: int
