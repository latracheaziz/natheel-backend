"""One independent Celery task per platform: publish_to_facebook, publish_to_instagram, ..."""
from __future__ import annotations

import asyncio
import uuid

from celery import Task

from app.core.config import get_settings
from app.core.database import worker_session
from app.core.encryption import get_cipher
from app.models.enums import Platform
from app.services.publication_executor import ExecutionOutcome, PublicationExecutor
from app.storage import get_storage
from app.workers.celery_app import celery_app


async def run_publication(publication_id: str, retries_so_far: int) -> ExecutionOutcome:
    settings = get_settings()
    async with worker_session() as session:
        executor = PublicationExecutor(session, get_storage(), get_cipher(), settings)
        return await executor.execute(uuid.UUID(publication_id), retries_so_far=retries_so_far)


def _make_task(platform: Platform):
    settings = get_settings()

    @celery_app.task(bind=True, name=f"publishing.publish_to_{platform.value}",
                     max_retries=settings.publish_max_retries)
    def publish(self: Task, publication_id: str) -> str:
        outcome = asyncio.run(run_publication(publication_id, self.request.retries))
        if outcome.status == "retry":
            # Exponential backoff computed by the executor; the DB row is already RETRYING.
            raise self.retry(countdown=outcome.retry_in_seconds)
        return outcome.status

    return publish


publish_to_facebook = _make_task(Platform.FACEBOOK)
publish_to_instagram = _make_task(Platform.INSTAGRAM)
publish_to_linkedin = _make_task(Platform.LINKEDIN)
publish_to_tiktok = _make_task(Platform.TIKTOK)
publish_to_youtube = _make_task(Platform.YOUTUBE)

PLATFORM_TASKS = {
    Platform.FACEBOOK.value: publish_to_facebook,
    Platform.INSTAGRAM.value: publish_to_instagram,
    Platform.LINKEDIN.value: publish_to_linkedin,
    Platform.TIKTOK.value: publish_to_tiktok,
    Platform.YOUTUBE.value: publish_to_youtube,
}
