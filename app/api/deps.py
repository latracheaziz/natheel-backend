"""Shared FastAPI dependencies: service factories and pagination."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.core.encryption import TokenCipher, get_cipher
from app.services.media_service import MediaService
from app.services.oauth_service import OAuthService
from app.services.post_service import PostService
from app.services.publishing_service import PublishingService
from app.services.scheduling_service import SchedulingService
from app.storage import get_storage
from app.storage.base import StorageBackend

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageBackend, Depends(get_storage)]
CipherDep = Annotated[TokenCipher, Depends(get_cipher)]


class Pagination:
    def __init__(self, limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0)) -> None:
        self.limit, self.offset = limit, offset


PaginationDep = Annotated[Pagination, Depends()]


def post_service(session: SessionDep, storage: StorageDep) -> PostService:
    return PostService(session, storage)


def media_service(session: SessionDep, storage: StorageDep, settings: SettingsDep) -> MediaService:
    return MediaService(session, storage, settings)


def publishing_service(session: SessionDep, storage: StorageDep, settings: SettingsDep) -> PublishingService:
    return PublishingService(session, storage, settings)


def scheduling_service(session: SessionDep, storage: StorageDep, settings: SettingsDep) -> SchedulingService:
    return SchedulingService(session, storage, settings)


def oauth_service(session: SessionDep, cipher: CipherDep, settings: SettingsDep) -> OAuthService:
    return OAuthService(session, cipher, settings)


PostServiceDep = Annotated[PostService, Depends(post_service)]
MediaServiceDep = Annotated[MediaService, Depends(media_service)]
PublishingServiceDep = Annotated[PublishingService, Depends(publishing_service)]
SchedulingServiceDep = Annotated[SchedulingService, Depends(scheduling_service)]
OAuthServiceDep = Annotated[OAuthService, Depends(oauth_service)]
