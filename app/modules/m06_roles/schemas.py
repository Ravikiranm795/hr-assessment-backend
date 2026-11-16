"""M6 request and response shapes."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RoleStatus = str  # draft | open | closed
Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleCreate(_Strict):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Backend Engineer",
                "department": "Platform",
                "experience_level": "mid",
                "vacancies": 2,
                "description": "Own our payment APIs.",
            }
        },
    )
    title: Text200
    department: OptText | None = None
    experience_level: OptText | None = None
    vacancies: Annotated[int, Field(strict=True, ge=1, le=1000)] = 1
    description: Annotated[str, Field(max_length=10000)] | None = None


class RolePatch(_Strict):
    title: Text200 | None = None
    department: OptText | None = None
    experience_level: OptText | None = None
    vacancies: Annotated[int, Field(strict=True, ge=1, le=1000)] | None = None
    description: Annotated[str, Field(max_length=10000)] | None = None


class ReassignIn(_Strict):
    owner_id: UUID


class LinkIn(_Strict):
    assessment_version_id: UUID


class LinkOut(BaseModel):
    id: UUID
    assessment_version_id: UUID
    assessment_id: UUID
    assessment_title: str
    version_no: int
    linked_by: UUID | None
    linked_at: datetime
    unlinked_at: datetime | None
    unlinked_by: UUID | None


class LinkHistory(BaseModel):
    items: list[LinkOut]


class RoleOut(BaseModel):
    id: UUID
    title: str
    department: str | None
    experience_level: str | None
    vacancies: int
    description: str | None
    owner_id: UUID
    owner_name: str | None
    owner_inactive: bool
    status: str
    closed_at: datetime | None
    linked_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class Progress(BaseModel):
    invites: dict[str, int]
    decisions: dict[str, int]


class RoleDetail(RoleOut):
    progress: Progress
    assessment: LinkOut | None
