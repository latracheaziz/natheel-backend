from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ScheduleStatus
from app.models.oauth_state import OAuthState
from app.models.scheduled_post import ScheduledPost


class ScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, schedule_id: uuid.UUID) -> ScheduledPost | None:
        return await self.session.get(ScheduledPost, schedule_id)

    async def list(self, *, status: str | None, post_id: uuid.UUID | None, limit: int,
                   offset: int) -> tuple[list[ScheduledPost], int]:
        stmt, count = select(ScheduledPost), select(func.count()).select_from(ScheduledPost)
        for column, value in ((ScheduledPost.status, status), (ScheduledPost.post_id, post_id)):
            if value:
                stmt, count = stmt.where(column == value), count.where(column == value)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(ScheduledPost.scheduled_at.desc()).limit(limit).offset(offset))
        return list(rows.scalars()), total

    async def claim_due(self, now: datetime, limit: int = 50) -> list[ScheduledPost]:
        """Locks due schedules (SKIP LOCKED: safe with several beat/worker processes) and marks them PROCESSING."""
        rows = await self.session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.status == ScheduleStatus.PENDING.value, ScheduledPost.scheduled_at <= now)
            .order_by(ScheduledPost.scheduled_at).limit(limit).with_for_update(skip_locked=True))
        due = list(rows.scalars())
        for schedule in due:
            schedule.status = ScheduleStatus.PROCESSING.value
        await self.session.commit()
        return due

    def add(self, schedule: ScheduledPost) -> None:
        self.session.add(schedule)


class OAuthStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, state: OAuthState) -> None:
        self.session.add(state)

    async def consume(self, state_hash: str) -> OAuthState | None:
        """Fetch and delete in one transaction: a state can be used exactly once."""
        row = (await self.session.execute(
            select(OAuthState).where(OAuthState.state_hash == state_hash).with_for_update())).scalar_one_or_none()
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()
        return row

    async def purge_expired(self, now: datetime) -> int:
        result = await self.session.execute(delete(OAuthState).where(OAuthState.expires_at < now))
        return result.rowcount or 0
