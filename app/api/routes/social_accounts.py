from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OAuthServiceDep, PaginationDep, SessionDep
from app.api.responses import ERRORS
from app.core.exceptions import NotFoundError
from app.models.social_account import SocialAccount
from app.repositories.social_account_repository import SocialAccountRepository
from app.schemas.common import Page
from app.schemas.social_account import SocialAccountRead

router = APIRouter(prefix="/social-accounts", tags=["Social accounts"], responses=ERRORS)


@router.get("", response_model=Page[SocialAccountRead], summary="List connected social accounts",
            description="Token material is never returned; `token_status` summarises expiry.")
async def list_accounts(session: SessionDep, page: PaginationDep, platform: str | None = None,
                        status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await SocialAccountRepository(session).list(
        platform=platform.lower() if platform else None, status=status_filter.upper() if status_filter else None,
        limit=page.limit, offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/{account_id}", response_model=SocialAccountRead, summary="Get a social account")
async def get_account(account_id: uuid.UUID, session: SessionDep) -> SocialAccount:
    account = await SocialAccountRepository(session).get(account_id)
    if account is None:
        raise NotFoundError("Social account not found.", code="SOCIAL_ACCOUNT_NOT_FOUND")
    return account


@router.delete("/{account_id}", response_model=SocialAccountRead, summary="Disconnect a social account",
               description="Revokes the token at the platform (best effort), then erases all stored credentials. "
                           "The row is kept with status DISCONNECTED so publication history stays intact.")
async def disconnect_account(account_id: uuid.UUID, service: OAuthServiceDep) -> SocialAccount:
    return await service.disconnect(account_id)
