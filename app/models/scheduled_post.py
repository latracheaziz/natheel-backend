from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.enums import ScheduleStatus


class ScheduledPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_posts"
    __table_args__ = (Index("ix_scheduled_posts_status_scheduled_at", "status", "scheduled_at"),)

    post_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)  # always UTC
    status: Mapped[str] = mapped_column(String(24), default=ScheduleStatus.PENDING.value, nullable=False)
    # Optional snapshot of the targets chosen when scheduling; falls back to Post.target_platforms.
    platforms: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(Text)

    post = relationship("Post", back_populates="schedules", lazy="raise")
