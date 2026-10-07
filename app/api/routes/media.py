from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status

from app.api.deps import MediaServiceDep, PaginationDep, SessionDep
from app.api.responses import ERRORS
from app.core.exceptions import NotFoundError
from app.core.rate_limit import rate_limit
from app.models.media import Media
from app.repositories.media_repository import MediaRepository
from app.schemas.common import Page
from app.schemas.media import MediaRead

router = APIRouter(prefix="/media", tags=["Media"], responses=ERRORS)


@router.post("", response_model=MediaRead, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(rate_limit("upload", "rate_limit_upload"))], summary="Upload an image or video",
             description="Accepts JPG/JPEG/PNG/WEBP images and MP4 videos. The file is validated by content "
                         "(magic bytes, decoded dimensions, MP4 structure); the client MIME type is ignored. "
                         "Optionally attach to a post with `post_id`.")
async def upload_media(service: MediaServiceDep, file: Annotated[UploadFile, File()],
                       post_id: Annotated[uuid.UUID | None, Form()] = None) -> Media:
    return await service.upload(file, post_id)


@router.get("", response_model=Page[MediaRead], summary="List media")
async def list_media(session: SessionDep, page: PaginationDep, post_id: uuid.UUID | None = None,
                     unattached: bool = False) -> dict:
    items, total = await MediaRepository(session).list(post_id=post_id, unattached=unattached,
                                                       limit=page.limit, offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/{media_id}", response_model=MediaRead, summary="Get media")
async def get_media(media_id: uuid.UUID, session: SessionDep) -> Media:
    media = await MediaRepository(session).get(media_id)
    if media is None:
        raise NotFoundError("Media not found.", code="MEDIA_NOT_FOUND")
    return media


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete media and its file")
async def delete_media(media_id: uuid.UUID, service: MediaServiceDep) -> Response:
    await service.delete(media_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
