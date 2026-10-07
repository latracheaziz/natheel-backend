from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.enums import PublicationStatus


class Publication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One post -> one social account delivery, tracked independently of the others."""

    __tablename__ = "publications"
    __table_args__ = (
        # Idempotency: a post can only have one delivery record per account.
        UniqueConstraint("post_id", "social_account_id", name="uq_publications_post_account"),
        Index("ix_publications_status_updated_at", "status", "updated_at"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("social_accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default=PublicationStatus.PENDING.value, nullable=False)
    external_post_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    post = relationship("Post", back_populates="publications", lazy="raise")
    social_account = relationship("SocialAccount", lazy="joined")
