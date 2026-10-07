from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PublicationStatus
from app.models.publication import Publication
from app.models.publishing_log import PublishingLog


class PublicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, publication_id: uuid.UUID, *, refresh: bool = False) -> Publication | None:
        stmt = select(Publication).where(Publication.id == publication_id)
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_post(self, post_id: uuid.UUID) -> list[Publication]:
        rows = await self.session.execute(
            select(Publication).where(Publication.post_id == post_id)
            .execution_options(populate_existing=True))
        return list(rows.scalars().unique())

    async def get_for_post_account(self, post_id: uuid.UUID, account_id: uuid.UUID) -> Publication | None:
        stmt = select(Publication).where(Publication.post_id == post_id,
                                         Publication.social_account_id == account_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, *, status: str | None, platform: str | None, post_id: uuid.UUID | None,
                   limit: int, offset: int) -> tuple[list[Publication], int]:
        stmt, count = select(Publication), select(func.count()).select_from(Publication)
        for column, value in ((Publication.status, status), (Publication.platform, platform),
                              (Publication.post_id, post_id)):
            if value:
                stmt, count = stmt.where(column == value), count.where(column == value)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(Publication.created_at.desc()).limit(limit).offset(offset))
        return list(rows.scalars().unique()), total

    async def claim(self, publication_id: uuid.UUID) -> bool:
        """Atomic compare-and-set QUEUED/RETRYING -> PUBLISHING.

        Returns False when another worker already claimed it (duplicate delivery) or the publication
        is no longer runnable, which makes task execution idempotent.
        """
        result = await self.session.execute(
            update(Publication)
            .where(Publication.id == publication_id,
                   Publication.status.in_([PublicationStatus.QUEUED.value, PublicationStatus.RETRYING.value]))
            .values(status=PublicationStatus.PUBLISHING.value, attempt_count=Publication.attempt_count + 1)
            .execution_options(synchronize_session=False))
        await self.session.commit()
        return result.rowcount == 1

    async def list_stale(self, before: datetime) -> list[Publication]:
        rows = await self.session.execute(
            select(Publication).where(Publication.status == PublicationStatus.PUBLISHING.value,
                                      Publication.updated_at < before))
        return list(rows.scalars().unique())

    def add(self, publication: Publication) -> None:
        self.session.add(publication)

    # ------------------------------------------------------------------ logs
    def add_log(self, log: PublishingLog) -> None:
        self.session.add(log)

    async def list_logs(self, *, publication_id: uuid.UUID | None, post_id: uuid.UUID | None,
                        level: str | None, platform: str | None, limit: int,
                        offset: int) -> tuple[list[PublishingLog], int]:
        stmt, count = select(PublishingLog), select(func.count()).select_from(PublishingLog)
        for column, value in ((PublishingLog.publication_id, publication_id), (PublishingLog.post_id, post_id),
                              (PublishingLog.level, level), (PublishingLog.platform, platform)):
            if value:
                stmt, count = stmt.where(column == value), count.where(column == value)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(PublishingLog.created_at.desc()).limit(limit).offset(offset))
        return list(rows.scalars()), total
