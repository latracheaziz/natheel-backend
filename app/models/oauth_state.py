from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, utcnow


class OAuthState(UUIDPrimaryKeyMixin, Base):
    """Short-lived, single-use CSRF state for an OAuth authorization round-trip.

    Only a SHA-256 hash of the state is stored.
    """

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
