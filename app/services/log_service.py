"""Publication audit logging: persisted (admin /logs) and emitted as structured JSON."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, redact_text
from app.models.publication import Publication
from app.models.publishing_log import PublishingLog

logger = get_logger("app.publishing")


def record_event(session: AsyncSession, publication: Publication, event: str, *, level: str = "INFO",
                 message: str | None = None, error_code: str | None = None) -> None:
    """Adds a log row to the current transaction (caller commits) and writes a JSON log line."""
    safe_message = redact_text(message)[:1000] if message else None
    session.add(PublishingLog(
        publication_id=publication.id, post_id=publication.post_id, platform=publication.platform,
        level=level, event=event, status=publication.status, attempt=publication.attempt_count,
        error_code=error_code, message=safe_message))
    logger.log(getattr(logging, level, logging.INFO), event, extra={
        "publication_id": str(publication.id), "post_id": str(publication.post_id),
        "platform": publication.platform, "attempt": publication.attempt_count,
        "status": publication.status, "error_code": error_code})
