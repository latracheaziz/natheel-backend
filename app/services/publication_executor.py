"""Worker-side execution of a single publication (one platform, one account).

Guarantees:
* idempotent  - an atomic QUEUED/RETRYING -> PUBLISHING claim means duplicate deliveries are no-ops;
* isolated    - a failure only affects its own publication; the post is aggregated (PARTIALLY_PUBLISHED...);
* classified  - retryable errors back off exponentially, permanent ones fail immediately.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import TokenCipher
from app.core.logging import get_logger
from app.integrations.social.base import AccountContext, ProviderError, TokenExpiredError
from app.integrations.social.registry import get_provider
from app.models.enums import PublicationStatus, check_publication_transition
from app.models.publication import Publication
from app.repositories.post_repository import PostRepository
from app.repositories.publication_repository import PublicationRepository
from app.services.log_service import record_event
from app.services.post_service import PostService
from app.services.publishing_service import build_content
from app.services.token_service import TokenService
from app.storage.base import StorageBackend

logger = get_logger(__name__)


@dataclass
class ExecutionOutcome:
    status: str                      # published | failed | retry | skipped
    retry_in_seconds: int | None = None
    error_code: str | None = None


def backoff_seconds(retries: int, settings: Settings) -> int:
    """Exponential backoff with +/-20% jitter: base * 2^retries, capped."""
    delay = min(settings.publish_backoff_base_seconds * (2 ** retries), settings.publish_backoff_max_seconds)
    return max(1, int(delay * random.uniform(0.8, 1.2)))


class PublicationExecutor:
    def __init__(self, session: AsyncSession, storage: StorageBackend, cipher: TokenCipher,
                 settings: Settings) -> None:
        self.session, self.storage, self.cipher, self.settings = session, storage, cipher, settings
        self.publications = PublicationRepository(session)
        self.posts = PostRepository(session)
        self.post_service = PostService(session, storage)
        self.tokens = TokenService(session, cipher, settings)

    async def execute(self, publication_id: uuid.UUID, *, retries_so_far: int = 0) -> ExecutionOutcome:
        if not await self.publications.claim(publication_id):
            logger.info("publication_skipped", extra={"publication_id": str(publication_id)})
            return ExecutionOutcome("skipped")

        pub = await self.publications.get(publication_id, refresh=True)
        assert pub is not None
        post_id = pub.post_id
        record_event(self.session, pub, "publication_started")
        await self.post_service.sync_status(post_id)
        await self.session.commit()

        try:
            outcome = await self._publish(pub, retries_so_far)
        except Exception as exc:  # last-resort guard: never leave the row stuck in PUBLISHING
            outcome = await self._handle_failure(publication_id, exc, retries_so_far)

        await self.post_service.sync_status(post_id)
        await self.session.commit()
        return outcome

    async def _publish(self, pub: Publication, retries_so_far: int) -> ExecutionOutcome:
        if pub.external_post_id:  # already created remotely by a previous attempt: just finalise
            return await self._mark_published(pub, pub.external_post_id)

        post = await self.posts.get(pub.post_id)
        account = pub.social_account
        if post is None:
            raise ProviderError("POST_NOT_FOUND", "The post no longer exists.")
        provider = get_provider(pub.platform)

        content = build_content(post, self.storage)
        problems = provider.validate_content(content)
        if problems:
            raise ProviderError(f"{pub.platform.upper()}_INVALID_CONTENT", "; ".join(problems))

        access_token = await self.tokens.get_access_token(account, provider)
        ctx = AccountContext(account_id=str(account.id), platform_account_id=account.platform_account_id,
                             access_token=access_token, metadata=dict(account.account_metadata or {}))
        result = await provider.publish(ctx, content)
        return await self._mark_published(pub, result.external_post_id)

    async def _mark_published(self, pub: Publication, external_id: str) -> ExecutionOutcome:
        check_publication_transition(PublicationStatus(pub.status), PublicationStatus.PUBLISHED)
        pub.status = PublicationStatus.PUBLISHED.value
        pub.external_post_id = external_id
        pub.published_at = datetime.now(timezone.utc)
        pub.error_code = pub.error_message = None
        record_event(self.session, pub, "publication_succeeded")
        await self.session.commit()
        return ExecutionOutcome("published")

    async def _handle_failure(self, publication_id: uuid.UUID, exc: Exception,
                              retries_so_far: int) -> ExecutionOutcome:
        await self.session.rollback()  # rollback expires ORM state; reload by id
        pub = await self.publications.get(publication_id, refresh=True)
        assert pub is not None

        if isinstance(exc, ProviderError):
            code, message, retryable = exc.code, exc.message, exc.retryable
        elif type(exc).__name__ == "SoftTimeLimitExceeded":
            code, message, retryable = "PUBLISH_TIMEOUT", "Publishing took too long.", True
        else:
            code, message, retryable = "INTERNAL_ERROR", "Unexpected error while publishing.", False
            logger.error("publication_unexpected_error", extra={
                "publication_id": str(pub.id), "error_type": type(exc).__name__}, exc_info=exc)

        pub.error_code, pub.error_message = code, message[:1000]
        if retryable and retries_so_far < self.settings.publish_max_retries:
            check_publication_transition(PublicationStatus(pub.status), PublicationStatus.RETRYING)
            pub.status = PublicationStatus.RETRYING.value
            delay = backoff_seconds(retries_so_far, self.settings)
            record_event(self.session, pub, "publication_retry_scheduled", level="WARNING", error_code=code,
                         message=f"{message} (retry in {delay}s)")
            await self.session.commit()
            return ExecutionOutcome("retry", retry_in_seconds=delay, error_code=code)

        check_publication_transition(PublicationStatus(pub.status), PublicationStatus.FAILED)
        pub.status = PublicationStatus.FAILED.value
        event = "publication_gave_up" if retryable else "publication_failed"
        record_event(self.session, pub, event, level="ERROR", error_code=code, message=message)
        await self.session.commit()
        return ExecutionOutcome("failed", error_code=code)


async def recover_stale_publications(session: AsyncSession, settings: Settings) -> int:
    """Marks publications stuck in PUBLISHING (e.g. worker killed) as FAILED.

    They are deliberately not re-sent automatically: the remote post may already exist, and an
    administrator must retry explicitly to avoid duplicate posts.
    """
    repo = PublicationRepository(session)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.stale_publication_minutes)
    stale = await repo.list_stale(cutoff)
    post_service = PostService(session, None)  # type: ignore[arg-type]
    for pub in stale:
        pub.status = PublicationStatus.FAILED.value
        pub.error_code = "PUBLISH_INTERRUPTED"
        pub.error_message = ("The worker stopped while publishing. Check the platform for the post, "
                             "then retry if it is missing.")
        record_event(session, pub, "publication_interrupted", level="ERROR", error_code="PUBLISH_INTERRUPTED")
        await session.flush()
        await post_service.sync_status(pub.post_id)
    await session.commit()
    return len(stale)
