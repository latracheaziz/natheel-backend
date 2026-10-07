from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.enums import (
    PUBLICATION_IN_FLIGHT, PostStatus, PublicationStatus, check_post_transition)
from app.models.post import Post
from app.repositories.media_repository import MediaRepository
from app.repositories.post_repository import PostRepository
from app.repositories.publication_repository import PublicationRepository
from app.schemas.post import PostCreate, PostUpdate
from app.services.media_service import EDITABLE_POST_STATUSES
from app.storage.base import StorageBackend


def aggregate_post_status(statuses: list[str]) -> PostStatus | None:
    """Derives a post's status from its publications. A single failed platform never fails the post."""
    if not statuses:
        return None
    current = {PublicationStatus(s) for s in statuses}
    if current & {PublicationStatus.PUBLISHING, PublicationStatus.RETRYING}:
        return PostStatus.PUBLISHING
    if current & PUBLICATION_IN_FLIGHT:
        return PostStatus.QUEUED
    published = statuses.count(PublicationStatus.PUBLISHED.value)
    if published == len(statuses):
        return PostStatus.PUBLISHED
    return PostStatus.PARTIALLY_PUBLISHED if published else PostStatus.FAILED


class PostService:
    def __init__(self, session: AsyncSession, storage: StorageBackend) -> None:
        self.session, self.storage = session, storage
        self.posts = PostRepository(session)
        self.media = MediaRepository(session)
        self.publications = PublicationRepository(session)

    async def get(self, post_id: uuid.UUID) -> Post:
        post = await self.posts.get(post_id)
        if post is None:
            raise NotFoundError("Post not found.", code="POST_NOT_FOUND")
        return post

    async def create(self, data: PostCreate) -> Post:
        post = Post(content=data.content, target_platforms=data.platforms, status=PostStatus.DRAFT.value)
        self.posts.add(post)
        await self.session.flush()
        await self._attach_media(post.id, data.media_ids)
        await self.session.commit()
        return await self._reload(post.id)

    async def update(self, post_id: uuid.UUID, data: PostUpdate) -> Post:
        post = await self.get(post_id)
        if post.status not in EDITABLE_POST_STATUSES:
            raise ConflictError(f"A post in status {post.status} cannot be edited.", code="POST_NOT_EDITABLE")
        if data.content is not None:
            post.content = data.content
        if data.platforms is not None:
            post.target_platforms = data.platforms
        if data.media_ids is not None:
            await self._attach_media(post.id, data.media_ids, replace=True)
        await self.session.commit()
        return await self._reload(post_id)

    async def delete(self, post_id: uuid.UUID) -> None:
        post = await self.get(post_id)
        if post.status in {PostStatus.QUEUED.value, PostStatus.PUBLISHING.value}:
            raise ConflictError("A post that is being published cannot be deleted.", code="POST_IN_PROGRESS")
        keys = [m.storage_key for m in post.media]
        await self.posts.delete(post)
        await self.session.commit()
        for key in keys:
            await self.storage.delete(key)

    async def sync_status(self, post_id: uuid.UUID) -> Post | None:
        """Recomputes the post status from its publications (row-locked to avoid racing workers)."""
        post = await self.posts.get(post_id, for_update=True)
        if post is None:
            return None
        pubs = await self.publications.list_for_post(post_id)
        target = aggregate_post_status([p.status for p in pubs])
        if target is not None and target.value != post.status:
            check_post_transition(PostStatus(post.status), target)
            post.status = target.value
        return post

    # ------------------------------------------------------------------ internals
    async def _attach_media(self, post_id: uuid.UUID, media_ids: list[uuid.UUID], *, replace: bool = False) -> None:
        wanted = list(dict.fromkeys(media_ids))
        items = await self.media.get_many(wanted)
        if len(items) != len(wanted):
            raise UnprocessableError("One or more media items do not exist.", code="MEDIA_NOT_FOUND")
        for item in items:
            if item.post_id not in (None, post_id):
                raise ConflictError("Media is already attached to another post.", code="MEDIA_IN_USE")
        if replace:
            current = await self.media.list(post_id=post_id, unattached=False, limit=1000, offset=0)
            for existing in current[0]:
                if existing.id not in wanted:
                    existing.post_id = None  # detach, keep the file in the media library
        for item in items:
            item.post_id = post_id
        await self.session.flush()

    async def _reload(self, post_id: uuid.UUID) -> Post:
        post = await self.posts.get(post_id, refresh=True)
        assert post is not None
        return post
