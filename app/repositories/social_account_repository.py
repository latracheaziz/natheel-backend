from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AccountStatus
from app.models.social_account import SocialAccount


class SocialAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, account_id: uuid.UUID, *, for_update: bool = False) -> SocialAccount | None:
        stmt = select(SocialAccount).where(SocialAccount.id == account_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_platform_account(self, platform: str, platform_account_id: str) -> SocialAccount | None:
        stmt = select(SocialAccount).where(SocialAccount.platform == platform,
                                           SocialAccount.platform_account_id == platform_account_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, *, platform: str | None = None, status: str | None = None, limit: int = 100,
                   offset: int = 0) -> tuple[list[SocialAccount], int]:
        stmt, count = select(SocialAccount), select(func.count()).select_from(SocialAccount)
        if platform:
            stmt, count = stmt.where(SocialAccount.platform == platform), count.where(SocialAccount.platform == platform)
        if status:
            stmt, count = stmt.where(SocialAccount.status == status), count.where(SocialAccount.status == status)
        total = (await self.session.execute(count)).scalar_one()
        rows = await self.session.execute(stmt.order_by(SocialAccount.platform, SocialAccount.connected_at)
                                          .limit(limit).offset(offset))
        return list(rows.scalars()), total

    async def list_connected(self, platform: str) -> list[SocialAccount]:
        rows = await self.session.execute(
            select(SocialAccount).where(SocialAccount.platform == platform,
                                        SocialAccount.status.in_([AccountStatus.CONNECTED.value,
                                                                  AccountStatus.EXPIRED.value]))
            .order_by(SocialAccount.connected_at))
        return list(rows.scalars())

    async def list_expiring(self, before: datetime) -> list[SocialAccount]:
        rows = await self.session.execute(
            select(SocialAccount).where(SocialAccount.status == AccountStatus.CONNECTED.value,
                                        SocialAccount.refresh_token_encrypted.is_not(None),
                                        SocialAccount.token_expires_at.is_not(None),
                                        SocialAccount.token_expires_at <= before))
        return list(rows.scalars())

    async def count_by_platform(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(SocialAccount.platform, func.count()).where(
                SocialAccount.status == AccountStatus.CONNECTED.value).group_by(SocialAccount.platform))
        return {platform: count for platform, count in rows.all()}

    def add(self, account: SocialAccount) -> None:
        self.session.add(account)
