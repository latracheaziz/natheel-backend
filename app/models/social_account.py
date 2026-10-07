from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, UTCDateTime, UUIDPrimaryKeyMixin, utcnow
from app.models.enums import AccountStatus


class SocialAccount(UUIDPrimaryKeyMixin, Base):
    """A connected social identity (Facebook Page, Instagram business account, LinkedIn member...).

    Tokens are only ever stored encrypted and are never serialised by any schema.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "platform_account_id", name="uq_social_accounts_platform_account"),
        Index("ix_social_accounts_platform_status", "platform", "status"),
    )

    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    # "metadata" is reserved by SQLAlchemy's declarative API, hence the attribute alias.
    account_metadata: Mapped[dict] = mapped_column("metadata", JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default=AccountStatus.CONNECTED.value, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # Convenience for services (not a column).
    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token_encrypted)

    def __repr__(self) -> str:  # never include token material
        return f"<SocialAccount {self.platform}:{self.platform_account_id} {self.status}>"
