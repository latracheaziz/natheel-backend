from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MediaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    post_id: uuid.UUID | None
    file_url: str
    original_filename: str | None
    file_type: str
    mime_type: str
    file_size: int
    metadata: dict = Field(validation_alias="file_metadata")
    created_at: datetime
