from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError
from app.models.media import Media
from app.models.enums import PostStatus
from app.repositories.media_repository import MediaRepository
from app.repositories.post_repository import PostRepository
from app.storage.base import StorageBackend
from app.utils.file_validation import validate_media_file

_READ_CHUNK = 1024 * 1024
EDITABLE_POST_STATUSES = {PostStatus.DRAFT.value, PostStatus.FAILED.value, PostStatus.SCHEDULED.value}


class MediaService:
    """Validates uploads by inspecting content, then hands the file to the storage backend."""

    def __init__(self, session: AsyncSession, storage: StorageBackend, settings: Settings) -> None:
        self.session, self.storage, self.settings = session, storage, settings
        self.media = MediaRepository(session)
        self.posts = PostRepository(session)

    async def upload(self, upload: UploadFile, post_id: uuid.UUID | None = None) -> Media:
        if post_id is not None:
            post = await self.posts.get(post_id)
            if post is None:
                raise NotFoundError("Post not found.", code="POST_NOT_FOUND")
            if post.status not in EDITABLE_POST_STATUSES:
                raise ConflictError("Media cannot be added to a post that is queued or published.",
                                    code="POST_NOT_EDITABLE")
        filename = upload.filename or ""
        hard_cap = max(self.settings.max_image_bytes, self.settings.max_video_bytes)

        fd, tmp_name = tempfile.mkstemp(prefix="upload_")
        tmp_path = Path(tmp_name)
        try:
            written = 0
            with os.fdopen(fd, "wb") as out:
                while chunk := await upload.read(_READ_CHUNK):
                    written += len(chunk)
                    if written > hard_cap:  # stop reading as soon as the absolute limit is crossed
                        raise PayloadTooLargeError("File exceeds the maximum allowed size.")
                    out.write(chunk)
            validated = await asyncio.to_thread(
                validate_media_file, tmp_path, filename,
                max_image_bytes=self.settings.max_image_bytes, max_video_bytes=self.settings.max_video_bytes,
                min_dimension=self.settings.image_min_dimension, max_dimension=self.settings.image_max_dimension)

            key = f"media/{validated.file_type.value}s/{uuid.uuid4().hex}.{validated.extension}"
            await self.storage.save(key, tmp_path, validated.mime_type)
        finally:
            tmp_path.unlink(missing_ok=True)

        media = Media(
            post_id=post_id, storage_key=key, file_url=self.storage.public_url(key),
            original_filename=Path(filename).name[:255] or None, file_type=validated.file_type.value,
            mime_type=validated.mime_type, file_size=written, file_metadata=validated.metadata)
        self.media.add(media)
        try:
            await self.session.commit()
        except Exception:
            await self.storage.delete(key)  # don't orphan the stored object
            raise
        return media

    async def delete(self, media_id: uuid.UUID) -> None:
        media = await self.media.get(media_id)
        if media is None:
            raise NotFoundError("Media not found.", code="MEDIA_NOT_FOUND")
        if media.post_id is not None:
            post = await self.posts.get(media.post_id)
            if post is not None and post.status not in EDITABLE_POST_STATUSES:
                raise ConflictError("Media cannot be removed from a post that is queued or published.",
                                    code="POST_NOT_EDITABLE")
        key = media.storage_key
        await self.media.delete(media)
        await self.session.commit()
        await self.storage.delete(key)
