from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

TokenStatus = Literal["valid", "expiring_soon", "expired", "no_expiry", "none"]


class SocialAccountRead(BaseModel):
    """Public view of a connection. Token fields are intentionally absent from this schema."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    platform: str
    platform_account_id: str
    username: str | None
    display_name: str | None
    token_expires_at: datetime | None
    scopes: list[str]
    metadata: dict = Field(validation_alias="account_metadata")
    status: str
    connected_at: datetime
    updated_at: datetime
    access_token_encrypted: str | None = Field(default=None, exclude=True, repr=False)
    has_refresh_token: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_status(self) -> TokenStatus:
        if not self.access_token_encrypted:
            return "none"
        if self.token_expires_at is None:
            return "no_expiry"
        now = datetime.now(timezone.utc)
        if self.token_expires_at <= now:
            return "expired"
        if self.token_expires_at - now < timedelta(days=7):
            return "expiring_soon"
        return "valid"


class SocialAccountBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: str
    username: str | None
    display_name: str | None
    status: str


class ConnectResponse(BaseModel):
    platform: str
    authorization_url: str
    expires_in: int
