from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel


class OAuthProviderInfo(BaseModel):
    platform: str
    configured: bool
    scopes: list[str]
    redirect_uri: str


class OAuthCallbackResult(BaseModel):
    status: Literal["connected", "error"]
    platform: str
    account_ids: list[uuid.UUID] = []
    error_code: str | None = None
