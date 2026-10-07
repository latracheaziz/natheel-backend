from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.enums import PostStatus, ScheduleStatus, check_post_transition
from app.models.scheduled_post import ScheduledPost
from app.repositories.schedule_repository import ScheduleRepository
from app.services.dispatcher import PublicationDispatcher
from app.services.post_service import PostService
from app.services.publishing_service import PublishingService
from app.storage.base import StorageBackend
from app.utils.validators import dedupe, parse_platform

logger = get_logger(__name__)
SCHEDULABLE_STATUSES = {PostStatus.DRAFT.value, PostStatus.FAILED.value, PostStatus.SCHEDULED.value}


class SchedulingService:
    def __init__(self, session: AsyncSession, storage: StorageBackend, settings: Settings,
                 dispatcher: PublicationDispatcher | None = None) -> None:
        self.session, self.storage, self.settings = session, storage, settings
        self.schedules = ScheduleRepository(session)
        self.post_service = PostService(session, storage)
        self.publishing = PublishingService(session, storage, settings, dispatcher)

    async def create(self, post_id: uuid.UUID, scheduled_at: datetime,
                     platforms: list[str] | None) -> ScheduledPost:
        post = await self.post_service.get(post_id)
        scheduled_at = scheduled_at.astimezone(timezone.utc)
        if scheduled_at <= datetime.now(timezone.utc):
            raise UnprocessableError("scheduled_at must be in the future.", code="SCHEDULE_IN_PAST")
        if post.status not in SCHEDULABLE_STATUSES:
            raise ConflictError(f"A post in status {post.status} cannot be scheduled.", code="POST_NOT_SCHEDULABLE")
        if any(s.status in (ScheduleStatus.PENDING.value, ScheduleStatus.PROCESSING.value) for s in post.schedules):
            raise ConflictError("This post already has a pending schedule. Cancel it first.",
                                code="SCHEDULE_ALREADY_EXISTS")

        targets = [parse_platform(p) for p in dedupe([p.lower() for p in (platforms or post.target_platforms)])]
        if not targets:
            raise UnprocessableError("No platforms selected for this scheduled post.", code="NO_PLATFORMS_SELECTED")
        if not post.content.strip() and not post.media:
            raise UnprocessableError("The post has neither text nor media.", code="EMPTY_POST")
        # Early feedback on capabilities/content. Accounts and tokens are re-validated at dispatch time.
        self.publishing.validate_content_for_platforms(post, targets)

        schedule = ScheduledPost(post_id=post.id, scheduled_at=scheduled_at, platforms=[t.value for t in targets],
                                 status=ScheduleStatus.PENDING.value)
        self.schedules.add(schedule)
        if post.status != PostStatus.SCHEDULED.value:
            check_post_transition(PostStatus(post.status), PostStatus.SCHEDULED)
            post.status = PostStatus.SCHEDULED.value
        await self.session.commit()
        return schedule

    async def cancel(self, schedule_id: uuid.UUID) -> ScheduledPost:
        schedule = await self.schedules.get(schedule_id)
        if schedule is None:
            raise NotFoundError("Schedule not found.", code="SCHEDULE_NOT_FOUND")
        if schedule.status != ScheduleStatus.PENDING.value:
            raise ConflictError("Only pending schedules can be cancelled.", code="SCHEDULE_NOT_CANCELLABLE")
        schedule.status = ScheduleStatus.CANCELLED.value
        post = await self.post_service.get(schedule.post_id)
        if post.status == PostStatus.SCHEDULED.value and not any(
                s.id != schedule.id and s.status == ScheduleStatus.PENDING.value for s in post.schedules):
            post.status = PostStatus.DRAFT.value
        await self.session.commit()
        return schedule

    async def dispatch_due(self, now: datetime | None = None) -> dict[str, int]:
        """Run by Celery beat: validates and dispatches every due schedule."""
        now = now or datetime.now(timezone.utc)
        due = await self.schedules.claim_due(now)
        result = {"dispatched": 0, "failed": 0}
        for claimed in due:
            schedule_id, post_id, platforms = claimed.id, claimed.post_id, list(claimed.platforms)
            try:
                await self.publishing.start_publishing(post_id, platforms or None)
            except AppError as exc:
                await self._fail(schedule_id, post_id, f"{exc.code}: {exc.message}")
                result["failed"] += 1
            except Exception as exc:
                logger.error("schedule_dispatch_error", extra={"schedule_id": str(schedule_id),
                                                               "error_type": type(exc).__name__}, exc_info=exc)
                await self._fail(schedule_id, post_id, "INTERNAL_ERROR: unexpected error while dispatching")
                result["failed"] += 1
            else:
                schedule = await self.schedules.get(schedule_id)
                schedule.status = ScheduleStatus.DISPATCHED.value  # type: ignore[union-attr]
                schedule.dispatched_at = datetime.now(timezone.utc)  # type: ignore[union-attr]
                await self.session.commit()
                result["dispatched"] += 1
        return result

    async def _fail(self, schedule_id: uuid.UUID, post_id: uuid.UUID, message: str) -> None:
        await self.session.rollback()
        schedule = await self.schedules.get(schedule_id)
        if schedule is not None:
            schedule.status = ScheduleStatus.FAILED.value
            schedule.error_message = message[:1000]
        post = await self.post_service.posts.get(post_id)
        if post is not None and post.status == PostStatus.SCHEDULED.value:
            post.status = PostStatus.FAILED.value
        await self.session.commit()
        logger.warning("schedule_failed", extra={"schedule_id": str(schedule_id), "post_id": str(post_id),
                                                 "error": message})
