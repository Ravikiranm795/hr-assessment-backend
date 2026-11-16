"""M12 shapes."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

GadgetSize = Literal["sm", "md", "lg"]


class CatalogItem(BaseModel):
    key: str
    title_key: str
    default_enabled: bool
    default_order: int


class CatalogOut(BaseModel):
    items: list[CatalogItem]


class LayoutItem(BaseModel):
    key: str
    size: GadgetSize = "md"
    enabled: bool = True


class LayoutIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "gadgets": [{"key": "summary_counts", "size": "lg"}, {"key": "open_roles", "enabled": False}]
            }
        },
    )
    gadgets: Annotated[list[LayoutItem], Field(max_length=40)]


class LayoutOut(BaseModel):
    items: list[LayoutItem]
    is_default: bool


class GadgetDataOut(BaseModel):
    key: str
    data: dict[str, Any]


class NotificationOut(BaseModel):
    id: UUID
    type: str
    payload: dict[str, Any]
    entity_type: str | None
    entity_id: UUID | None
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


class ReadAllOut(BaseModel):
    updated: int
