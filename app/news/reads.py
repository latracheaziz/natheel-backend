"""Per-admin read/unread state for Ministry of Tourism news.

Only the fact that a given admin read a given article is stored — never the article itself, which
always comes from the CMS. Rows are keyed by the admin's id, so two admins never see each other's
reading state. Schema creation follows the same `CREATE TABLE IF NOT EXISTS` approach as the
reviews module, which is how this project provisions its tables.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import get_sessionmaker
from app.core.logging import get_logger

logger = get_logger(__name__)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS news_reads (
        admin_id UUID NOT NULL,
        article_id TEXT NOT NULL,
        read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (admin_id, article_id)
    )
    """,
)


def _admin_uuid(admin_id: str) -> UUID | None:
    try:
        return UUID(str(admin_id))
    except (ValueError, AttributeError, TypeError):
        return None


_schema_ready = False


async def _ensure_schema(session) -> None:
    """Create the table once per process.

    The admin page polls for new articles, so paying a `CREATE TABLE IF NOT EXISTS` round trip
    on every request would add latency to every poll for no benefit.
    """
    global _schema_ready
    if _schema_ready:
        return
    for statement in SCHEMA_STATEMENTS:
        await session.execute(text(statement))
    await session.commit()
    _schema_ready = True


async def read_ids(admin_id: str) -> set[str] | None:
    """Article ids this admin marked as read, or None when the store cannot be reached.

    None is kept distinct from "nothing read yet" so the API can say its read counts are
    unavailable instead of reporting every article as unread.
    """
    admin = _admin_uuid(admin_id)
    if admin is None:
        return None
    try:
        async with get_sessionmaker()() as session:
            await _ensure_schema(session)
            result = await session.execute(
                text("SELECT article_id FROM news_reads WHERE admin_id = :admin_id"),
                {"admin_id": admin},
            )
            return {row.article_id for row in result}
    except SQLAlchemyError as exc:
        # The feed itself is still worth showing, so this degrades instead of failing the request.
        logger.warning("news read state unavailable", extra={"error": type(exc).__name__})
        return None


async def set_read(admin_id: str, article_id: str, read: bool) -> bool:
    """Mark one article read or unread for this admin. Returns the stored state."""
    admin = _admin_uuid(admin_id)
    if admin is None:
        return False
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        if read:
            await session.execute(
                text(
                    """
                    INSERT INTO news_reads (admin_id, article_id)
                    VALUES (:admin_id, :article_id)
                    ON CONFLICT (admin_id, article_id) DO NOTHING
                    """
                ),
                {"admin_id": admin, "article_id": article_id},
            )
        else:
            await session.execute(
                text("DELETE FROM news_reads WHERE admin_id = :admin_id AND article_id = :article_id"),
                {"admin_id": admin, "article_id": article_id},
            )
        await session.commit()
    return read
