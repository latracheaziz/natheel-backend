from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, UTCDateTime, UUIDPrimaryKeyMixin, utcnow
from datetime import datetime


class Media(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "media"

    # Nullable: files can be uploaded first and attached to a post afterwards.
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # image | video
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_metadata: Mapped[dict] = mapped_column("metadata", JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    post = relationship("Post", back_populates="media", lazy="raise")
