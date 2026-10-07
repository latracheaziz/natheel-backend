"""Periodic tasks driven by Celery beat."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.database import worker_session
from app.core.encryption import get_cipher
from app.core.logging import get_logger
from app.integrations.social.base import ProviderError
from app.integrations.social.registry import get_provider
from app.repositories.social_account_repository import SocialAccountRepository
from app.services.publication_executor import recover_stale_publications as _recover_stale
from app.services.scheduling_service import SchedulingService
from app.services.token_service import TokenService
from app.storage import get_storage
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _dispatch_due() -> dict[str, int]:
    async with worker_session() as session:
        return await SchedulingService(session, get_storage(), get_settings()).dispatch_due()


async def _refresh_tokens() -> int:
    settings = get_settings()
    refreshed = 0
    async with worker_session() as session:
        tokens = TokenService(session, get_cipher(), settings)
        horizon = datetime.now(timezone.utc) + timedelta(hours=24)
        for account in await SocialAccountRepository(session).list_expiring(horizon):
            account_id, platform = account.id, account.platform
            try:
                await tokens.refresh(account, get_provider(platform))
                refreshed += 1
            except ProviderError as exc:
                logger.warning("token_refresh_failed", extra={"platform": platform, "account_id": str(account_id),
                                                              "error_code": exc.code})
                await session.rollback()
    return refreshed


async def _recover() -> int:
    async with worker_session() as session:
        return await _recover_stale(session, get_settings())


@celery_app.task(name="scheduling.dispatch_due_schedules")
def dispatch_due_schedules() -> dict[str, int]:
    return asyncio.run(_dispatch_due())


@celery_app.task(name="scheduling.refresh_expiring_tokens")
def refresh_expiring_tokens() -> int:
    return asyncio.run(_refresh_tokens())


@celery_app.task(name="scheduling.recover_stale_publications")
def recover_stale_publications() -> int:
    return asyncio.run(_recover())
