from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PostStatus


class Post(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "posts"
    __table_args__ = (Index("ix_posts_status_created_at", "status", "created_at"),)

    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=PostStatus.DRAFT.value, nullable=False)
    # Platforms this post is intended for (used by "publish" without a body and by schedules).
    target_platforms: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    media = relationship("Media", back_populates="post", lazy="selectin", order_by="Media.created_at",
                         cascade="all, delete-orphan", passive_deletes=True)
    publications = relationship("Publication", back_populates="post", lazy="selectin",
                                order_by="Publication.created_at", cascade="all, delete-orphan",
                                passive_deletes=True)
    schedules = relationship("ScheduledPost", back_populates="post", lazy="selectin",
                             order_by="ScheduledPost.scheduled_at", cascade="all, delete-orphan",
                             passive_deletes=True)
