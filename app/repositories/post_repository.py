from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.post import Post


class PostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, post_id: uuid.UUID, *, refresh: bool = False, for_update: bool = False) -> Post | None:
        stmt = select(Post).where(Post.id == post_id)
        if refresh or for_update:
            stmt = stmt.execution_options(populate_existing=True)
        if for_update:  # serialises concurrent status aggregation for the same post
            stmt = stmt.with_for_update(of=Post)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, *, status: str | None, limit: int, offset: int) -> tuple[list[Post], int]:
        stmt = select(Post)
        count = select(func.count()).select_from(Post)
        if status:
            stmt, count = stmt.where(Post.status == status), count.where(Post.status == status)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(Post.created_at.desc()).limit(limit).offset(offset))
        return list(rows.scalars().unique()), total

    def add(self, post: Post) -> None:
        self.session.add(post)

    async def delete(self, post: Post) -> None:
        await self.session.delete(post)
