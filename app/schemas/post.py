from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import Platform
from app.schemas.media import MediaRead
from app.schemas.publication import PublicationRead
from app.schemas.schedule import ScheduleRead

MAX_CONTENT_LENGTH = 63_206  # hard ceiling (Facebook); per-platform limits are checked on publish


def _normalise_platforms(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    seen: list[str] = []
    for item in value:
        platform = Platform(item.lower())  # raises ValueError -> 422 for unknown platforms
        if platform.value not in seen:
            seen.append(platform.value)
    return seen


class PostCreate(BaseModel):
    content: str = Field(default="", max_length=MAX_CONTENT_LENGTH, examples=["New product launch 🚀"])
    platforms: list[str] = Field(default_factory=list, examples=[["facebook", "linkedin"]])
    media_ids: list[uuid.UUID] = Field(default_factory=list, description="Previously uploaded media to attach.")

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, value: list[str]) -> list[str]:
        return _normalise_platforms(value) or []


class PostUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    platforms: list[str] | None = None
    media_ids: list[uuid.UUID] | None = Field(
        default=None, description="Replaces the attached media set (order is preserved by upload time).")

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, value: list[str] | None) -> list[str] | None:
        return _normalise_platforms(value)


class PostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    status: str
    target_platforms: list[str]
    created_at: datetime
    updated_at: datetime
    media: list[MediaRead] = []
    publications: list[PublicationRead] = []
    schedules: list[ScheduleRead] = []
