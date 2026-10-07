from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScheduleCreate(BaseModel):
    post_id: uuid.UUID
    scheduled_at: datetime = Field(examples=["2026-10-10T18:00:00+01:00"])
    platforms: list[str] | None = Field(
        default=None, description="Defaults to the post's target platforms.")

    @field_validator("scheduled_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_at must include a timezone offset (e.g. +01:00 or Z).")
        return value.astimezone(timezone.utc)


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    scheduled_at: datetime
    status: str
    platforms: list[str]
    dispatched_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
