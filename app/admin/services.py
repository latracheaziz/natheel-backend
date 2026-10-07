"""Admin read-models: dashboard, platform status, system health."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.rate_limit import get_redis
from app.integrations.social.registry import all_providers
from app.models.enums import AccountStatus, ScheduleStatus
from app.models.post import Post
from app.models.publication import Publication
from app.models.scheduled_post import ScheduledPost
from app.models.social_account import SocialAccount
from app.storage.base import StorageBackend


class AdminService:
    def __init__(self, session: AsyncSession, settings: Settings, storage: StorageBackend) -> None:
        self.session, self.settings, self.storage = session, settings, storage

    async def _counts(self, column) -> dict[str, int]:
        rows = await self.session.execute(select(column, func.count()).group_by(column))
        return {key: count for key, count in rows.all()}

    async def dashboard(self) -> dict:
        recent_failures = (await self.session.execute(
            select(Publication).where(Publication.status == "FAILED")
            .order_by(Publication.updated_at.desc()).limit(10))).scalars().unique().all()
        pending_schedules = (await self.session.execute(
            select(func.count()).select_from(ScheduledPost)
            .where(ScheduledPost.status == ScheduleStatus.PENDING.value))).scalar_one()
        next_schedule = (await self.session.execute(
            select(func.min(ScheduledPost.scheduled_at)).where(ScheduledPost.status == ScheduleStatus.PENDING.value)
        )).scalar_one()
        return {
            "posts_by_status": await self._counts(Post.status),
            "publications_by_status": await self._counts(Publication.status),
            "accounts_by_status": await self._counts(SocialAccount.status),
            "pending_schedules": pending_schedules,
            "next_scheduled_at": next_schedule,
            "recent_failures": [
                {"publication_id": p.id, "post_id": p.post_id, "platform": p.platform,
                 "error_code": p.error_code, "error_message": p.error_message, "updated_at": p.updated_at}
                for p in recent_failures],
            "sections": ["social-accounts", "posts", "media", "schedules", "publications", "logs",
                         "platform-status", "health"],
        }

    async def platform_status(self) -> list[dict]:
        rows = await self.session.execute(
            select(SocialAccount.platform, SocialAccount.status, func.count())
            .group_by(SocialAccount.platform, SocialAccount.status))
        counts: dict[str, dict[str, int]] = {}
        for platform, status, count in rows.all():
            counts.setdefault(platform, {})[status] = count
        result = []
        for provider in all_providers():
            platform = provider.platform.value
            per_status = counts.get(platform, {})
            result.append({
                "platform": platform,
                "oauth_configured": provider.is_configured(),
                "redirect_uri": self.settings.oauth_redirect_uri(platform),
                "scopes": provider.scopes,
                "capabilities": asdict(provider.capabilities),
                "connected_accounts": per_status.get(AccountStatus.CONNECTED.value, 0),
                "expired_accounts": per_status.get(AccountStatus.EXPIRED.value, 0),
            })
        return result

    async def health(self) -> dict:
        checks: dict[str, dict] = {}

        try:
            await self.session.execute(text("SELECT 1"))
            checks["database"] = {"status": "ok"}
        except Exception as exc:
            checks["database"] = {"status": "error", "error": type(exc).__name__}

        try:
            await get_redis().ping()
            checks["redis"] = {"status": "ok"}
        except Exception as exc:
            checks["redis"] = {"status": "error", "error": type(exc).__name__}

        checks["worker"] = await asyncio.to_thread(self._worker_check) if checks["redis"]["status"] == "ok" \
            else {"status": "unknown"}

        try:
            await self.storage.size("__healthcheck__")
        except FileNotFoundError:
            checks["storage"] = {"status": "ok"}
        except Exception as exc:
            checks["storage"] = {"status": "error", "error": type(exc).__name__}
        else:
            checks["storage"] = {"status": "ok"}

        overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
        return {"status": overall, "checked_at": datetime.now(timezone.utc), "checks": checks}

    @staticmethod
    def _worker_check() -> dict:
        try:
            from app.workers.celery_app import celery_app

            replies = celery_app.control.inspect(timeout=1.0).ping() or {}
            return {"status": "ok" if replies else "error", "workers": len(replies)}
        except Exception as exc:
            return {"status": "error", "error": type(exc).__name__}
