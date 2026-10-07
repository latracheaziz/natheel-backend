from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, UUIDPrimaryKeyMixin, utcnow


class PublishingLog(UUIDPrimaryKeyMixin, Base):
    """Append-only audit trail of publication events (survives post deletion: no foreign keys)."""

    __tablename__ = "publishing_logs"
    __table_args__ = (Index("ix_publishing_logs_publication_created", "publication_id", "created_at"),)

    publication_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    post_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str | None] = mapped_column(String(24))
    attempt: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
