from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import PaginationDep, PublishingServiceDep, SessionDep
from app.api.responses import ERRORS
from app.core.exceptions import NotFoundError
from app.core.rate_limit import rate_limit
from app.models.publication import Publication
from app.repositories.publication_repository import PublicationRepository
from app.schemas.common import Page
from app.schemas.publication import (
    PublicationBrief, PublicationRead, PublishingLogRead, PublishRequest, PublishResponse)

router = APIRouter(tags=["Publishing"], responses=ERRORS)
_publish_limit = Depends(rate_limit("publish", "rate_limit_publish"))


@router.post("/posts/{post_id}/publish", response_model=PublishResponse, dependencies=[_publish_limit],
             summary="Publish a post to one or more platforms",
             description="Validates the post, media, connected accounts, tokens and per-platform capabilities, "
                         "creates one Publication per account and queues an independent background job for each. "
                         "Returns immediately; poll `GET /api/publications` for progress. Nothing is queued if any "
                         "selected platform fails validation.")
async def publish_post(post_id: uuid.UUID, service: PublishingServiceDep,
                       body: PublishRequest | None = None) -> PublishResponse:
    body = body or PublishRequest()
    _, publications = await service.start_publishing(post_id, body.platforms, body.account_ids)
    return PublishResponse(post_id=post_id, publications=[
        PublicationBrief(id=p.id, platform=p.platform, status=p.status) for p in publications])


@router.get("/publications", response_model=Page[PublicationRead], summary="Publishing history")
async def list_publications(session: SessionDep, page: PaginationDep, platform: str | None = None,
                            post_id: uuid.UUID | None = None,
                            status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await PublicationRepository(session).list(
        status=status_filter.upper() if status_filter else None, platform=platform.lower() if platform else None,
        post_id=post_id, limit=page.limit, offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/publications/{publication_id}", response_model=PublicationRead, summary="Get a publication")
async def get_publication(publication_id: uuid.UUID, session: SessionDep) -> Publication:
    pub = await PublicationRepository(session).get(publication_id)
    if pub is None:
        raise NotFoundError("Publication not found.", code="PUBLICATION_NOT_FOUND")
    return pub


@router.post("/publications/{publication_id}/retry", response_model=PublicationRead, dependencies=[_publish_limit],
             summary="Retry a failed publication",
             description="Only FAILED publications can be retried. Content, account and token are re-validated.")
async def retry_publication(publication_id: uuid.UUID, service: PublishingServiceDep) -> Publication:
    return await service.retry_publication(publication_id)


@router.get("/publications/{publication_id}/logs", response_model=Page[PublishingLogRead],
            summary="Event log of a single publication")
async def publication_logs(publication_id: uuid.UUID, session: SessionDep, page: PaginationDep) -> dict:
    items, total = await PublicationRepository(session).list_logs(
        publication_id=publication_id, post_id=None, level=None, platform=None, limit=page.limit, offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}
