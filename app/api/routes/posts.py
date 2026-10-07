from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import PaginationDep, PostServiceDep, SessionDep
from app.api.responses import ERRORS
from app.models.post import Post
from app.repositories.post_repository import PostRepository
from app.schemas.common import Page
from app.schemas.post import PostCreate, PostRead, PostUpdate

router = APIRouter(prefix="/posts", tags=["Posts"], responses=ERRORS)


@router.post("", response_model=PostRead, status_code=status.HTTP_201_CREATED, summary="Create a post",
             description="Creates a DRAFT post. Attach previously uploaded media via `media_ids` and choose "
                         "target `platforms` (facebook, instagram, linkedin, tiktok, youtube).")
async def create_post(body: PostCreate, service: PostServiceDep) -> Post:
    return await service.create(body)


@router.get("", response_model=Page[PostRead], summary="List posts")
async def list_posts(session: SessionDep, page: PaginationDep,
                     status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await PostRepository(session).list(status=status_filter.upper() if status_filter else None,
                                                      limit=page.limit, offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/{post_id}", response_model=PostRead, summary="Get a post with media, publications and schedules")
async def get_post(post_id: uuid.UUID, service: PostServiceDep) -> Post:
    return await service.get(post_id)


@router.patch("/{post_id}", response_model=PostRead, summary="Update a post",
              description="Only DRAFT, FAILED and SCHEDULED posts can be edited.")
async def update_post(post_id: uuid.UUID, body: PostUpdate, service: PostServiceDep) -> Post:
    return await service.update(post_id, body)


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a post",
               description="Not allowed while the post is QUEUED or PUBLISHING. Deletes attached media files.")
async def delete_post(post_id: uuid.UUID, service: PostServiceDep) -> Response:
    await service.delete(post_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
