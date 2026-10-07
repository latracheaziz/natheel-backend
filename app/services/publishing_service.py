"""Publishing orchestration (API side): validate -> create publications -> queue. Never calls social APIs."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, NotFoundError, UnprocessableError
from app.integrations.social.base import MediaItem, PublishContent, SocialMediaProvider
from app.integrations.social.registry import get_provider
from app.models.enums import (
    AccountStatus, MediaType, Platform, PostStatus, PublicationStatus, PUBLICATION_IN_FLIGHT,
    check_post_transition, check_publication_transition)
from app.models.post import Post
from app.models.publication import Publication
from app.models.social_account import SocialAccount
from app.repositories.post_repository import PostRepository
from app.repositories.publication_repository import PublicationRepository
from app.repositories.social_account_repository import SocialAccountRepository
from app.services.dispatcher import PublicationDispatcher, get_dispatcher
from app.services.log_service import record_event
from app.services.post_service import PostService
from app.services.token_service import token_is_usable
from app.storage.base import StorageBackend
from app.utils.validators import dedupe, parse_platform

DISPATCH_FAILED = "QUEUE_UNAVAILABLE"


def build_content(post: Post, storage: StorageBackend) -> PublishContent:
    media = [MediaItem(id=str(m.id), media_type=MediaType(m.file_type), mime_type=m.mime_type, size=m.file_size,
                       storage_key=m.storage_key, public_url=storage.public_url(m.storage_key), storage=storage)
             for m in post.media]
    return PublishContent(text=post.content or "", media=media)


class PublishingService:
    def __init__(self, session: AsyncSession, storage: StorageBackend, settings: Settings,
                 dispatcher: PublicationDispatcher | None = None) -> None:
        self.session, self.storage, self.settings = session, storage, settings
        self.dispatcher = dispatcher or get_dispatcher()
        self.posts = PostRepository(session)
        self.publications = PublicationRepository(session)
        self.accounts = SocialAccountRepository(session)
        self.post_service = PostService(session, storage)

    # ------------------------------------------------------------------ validation (no side effects)
    def validate_content_for_platforms(self, post: Post, platforms: list[Platform]) -> None:
        """Capability + content + media validation for every selected platform (all-or-nothing)."""
        content = build_content(post, self.storage)
        problems: dict[str, list[str]] = {}
        for platform in platforms:
            issues = get_provider(platform).validate_content(content)
            if issues:
                problems[platform.value] = issues
        if problems:
            raise UnprocessableError("The post cannot be published to the selected platforms.",
                                     code="CONTENT_VALIDATION_FAILED", details=problems)

    async def resolve_accounts(self, platforms: list[Platform],
                               account_ids: list[uuid.UUID] | None) -> list[SocialAccount]:
        chosen: list[SocialAccount] = []
        for platform in platforms:
            candidates = await self.accounts.list_connected(platform.value)
            if not candidates:
                raise ConflictError(f"No {platform.value.capitalize()} account is connected.",
                                    code=f"{platform.value.upper()}_NOT_CONNECTED")
            # account_ids pins accounts; ids that belong to other platforms are simply ignored here.
            pinned = [a for a in candidates if account_ids and a.id in account_ids]
            if pinned:
                candidates = pinned
            elif len(candidates) > 1:
                raise ConflictError(
                    f"Several {platform.value.capitalize()} accounts are connected; choose one with account_ids.",
                    code="AMBIGUOUS_ACCOUNT", details=[str(a.id) for a in candidates])
            for account in candidates:
                if not token_is_usable(account):
                    raise ConflictError(f"The {platform.value.capitalize()} connection has expired.",
                                        code=f"{platform.value.upper()}_TOKEN_EXPIRED")
                chosen.append(account)
        return chosen

    # ------------------------------------------------------------------ publish
    async def start_publishing(self, post_id: uuid.UUID, platforms: list[str] | None,
                               account_ids: list[uuid.UUID] | None = None) -> tuple[Post, list[Publication]]:
        post = await self.post_service.get(post_id)
        raw = platforms if platforms else post.target_platforms
        selected = [parse_platform(p) for p in dedupe([p.lower() for p in raw])]
        if not selected:
            raise UnprocessableError("No platforms selected.", code="NO_PLATFORMS_SELECTED")
        if not post.content.strip() and not post.media:
            raise UnprocessableError("The post has neither text nor media.", code="EMPTY_POST")

        self.validate_content_for_platforms(post, selected)           # capabilities + content + media
        accounts = await self.resolve_accounts(selected, account_ids)  # connected accounts + token check

        publications: list[Publication] = []
        for account in accounts:
            existing = await self.publications.get_for_post_account(post.id, account.id)
            if existing is None:
                pub = Publication(post_id=post.id, social_account_id=account.id, platform=account.platform,
                                  status=PublicationStatus.PENDING.value)
                self.publications.add(pub)
                await self.session.flush()
            elif existing.status == PublicationStatus.FAILED.value:
                pub = existing
            elif existing.status == PublicationStatus.PUBLISHED.value:
                raise ConflictError(f"Already published to {account.platform.capitalize()}.",
                                    code="ALREADY_PUBLISHED")
            else:
                raise ConflictError(f"A publication to {account.platform.capitalize()} is already in progress.",
                                    code="PUBLICATION_IN_PROGRESS")
            self._queue(pub)
            publications.append(pub)

        if post.status != PostStatus.QUEUED.value:
            check_post_transition(PostStatus(post.status), PostStatus.QUEUED)
            post.status = PostStatus.QUEUED.value
        for pub in publications:
            record_event(self.session, pub, "publication_queued")
        await self.session.commit()  # commit BEFORE dispatch so the worker always sees the rows

        await self._dispatch_all(publications)
        await self.post_service.sync_status(post_id)  # reflect any dispatch failure
        await self.session.commit()
        return await self._fresh(post_id), publications

    async def retry_publication(self, publication_id: uuid.UUID) -> Publication:
        pub = await self.publications.get(publication_id)
        if pub is None:
            raise NotFoundError("Publication not found.", code="PUBLICATION_NOT_FOUND")
        if pub.status != PublicationStatus.FAILED.value:
            raise ConflictError("Only failed publications can be retried.", code="PUBLICATION_NOT_RETRYABLE")
        post = await self.post_service.get(pub.post_id)
        platform = Platform(pub.platform)
        self.validate_content_for_platforms(post, [platform])
        account = pub.social_account
        if not token_is_usable(account):
            raise ConflictError(f"The {platform.value.capitalize()} connection has expired. Reconnect it first.",
                                code=f"{platform.value.upper()}_TOKEN_EXPIRED")
        self._queue(pub)
        record_event(self.session, pub, "publication_retry_requested")
        await self.session.flush()
        await self.post_service.sync_status(post.id)
        await self.session.commit()
        await self._dispatch_all([pub])
        await self.post_service.sync_status(post.id)
        await self.session.commit()
        return pub

    # ------------------------------------------------------------------ internals
    def _queue(self, pub: Publication) -> None:
        check_publication_transition(PublicationStatus(pub.status), PublicationStatus.QUEUED)
        pub.status = PublicationStatus.QUEUED.value
        pub.error_code = None
        pub.error_message = None

    async def _dispatch_all(self, publications: list[Publication]) -> None:
        for pub in publications:
            try:
                await self.dispatcher.dispatch(pub.id, pub.platform)
            except Exception as exc:  # broker down: don't leave a publication queued forever
                check_publication_transition(PublicationStatus(pub.status), PublicationStatus.FAILED)
                pub.status = PublicationStatus.FAILED.value
                pub.error_code = DISPATCH_FAILED
                pub.error_message = "The job queue is unavailable. Retry the publication shortly."
                record_event(self.session, pub, "dispatch_failed", level="ERROR", error_code=DISPATCH_FAILED,
                             message=type(exc).__name__)
        await self.session.commit()

    async def _fresh(self, post_id: uuid.UUID) -> Post:
        post = await self.posts.get(post_id, refresh=True)
        assert post is not None
        return post
