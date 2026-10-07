from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import Media


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, media_id: uuid.UUID) -> Media | None:
        return await self.session.get(Media, media_id)

    async def get_many(self, ids: list[uuid.UUID]) -> list[Media]:
        if not ids:
            return []
        rows = await self.session.execute(select(Media).where(Media.id.in_(ids)).order_by(Media.created_at))
        return list(rows.scalars())

    async def list(self, *, post_id: uuid.UUID | None, unattached: bool, limit: int,
                   offset: int) -> tuple[list[Media], int]:
        stmt, count = select(Media), select(func.count()).select_from(Media)
        if post_id:
            stmt, count = stmt.where(Media.post_id == post_id), count.where(Media.post_id == post_id)
        if unattached:
            stmt, count = stmt.where(Media.post_id.is_(None)), count.where(Media.post_id.is_(None))
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(Media.created_at.desc()).limit(limit).offset(offset))
        return list(rows.scalars()), total

    def add(self, media: Media) -> None:
        self.session.add(media)

    async def delete(self, media: Media) -> None:
        await self.session.delete(media)
