from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.social_account import SocialAccountBrief


class PublicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    status: str
    external_post_id: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    social_account: SocialAccountBrief | None = None


class PublishRequest(BaseModel):
    """If `platforms` is omitted the post's own target platforms are used."""

    platforms: list[str] | None = Field(default=None, examples=[["facebook", "instagram", "linkedin"]])
    account_ids: list[uuid.UUID] | None = Field(
        default=None, description="Pin specific connected accounts when a platform has more than one.")


class PublicationBrief(BaseModel):
    id: uuid.UUID
    platform: str
    status: str


class PublishResponse(BaseModel):
    message: str = "Publishing started"
    post_id: uuid.UUID
    publications: list[PublicationBrief]


class PublishingLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID | None
    post_id: uuid.UUID | None
    platform: str | None
    level: str
    event: str
    status: str | None
    attempt: int | None
    error_code: str | None
    message: str | None
    created_at: datetime
