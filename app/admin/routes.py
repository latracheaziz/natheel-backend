"""/adminnatheel/ - the protected administrative surface (JSON API consumed by the React admin)."""
from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.admin.auth import (
    AdminDep, check_login, clear_session_cookie, require_admin, set_session_cookie)
from app.admin.services import AdminService
from app.api.deps import (
    OAuthServiceDep, PaginationDep, PublishingServiceDep, SessionDep, SettingsDep, StorageDep)
from app.api.responses import ERRORS
from app.core.exceptions import AppError, NotFoundError
from app.core.rate_limit import rate_limit
from app.models.enums import Platform
from app.repositories.media_repository import MediaRepository
from app.repositories.post_repository import PostRepository
from app.repositories.publication_repository import PublicationRepository
from app.repositories.schedule_repository import ScheduleRepository
from app.repositories.social_account_repository import SocialAccountRepository
from app.schemas.common import MessageResponse, Page
from app.schemas.media import MediaRead
from app.schemas.post import PostRead
from app.schemas.publication import PublicationRead, PublishingLogRead
from app.schemas.schedule import ScheduleRead
from app.schemas.social_account import ConnectResponse, SocialAccountRead
from app.utils.validators import parse_platform

# --- public: only the login/logout endpoints -------------------------------------------------
public_router = APIRouter(prefix="/adminnatheel", tags=["Admin: session"])


class LoginRequest(BaseModel):
    username: str
    password: str


@public_router.post("/login", response_model=MessageResponse, summary="Start an admin session",
                    dependencies=[Depends(rate_limit("admin_login", "rate_limit_login"))],
                    description="Sets a signed, HTTP-only session cookie. HTTP Basic authentication is also "
                                "accepted on every admin endpoint.")
async def login(body: LoginRequest, response: Response, settings: SettingsDep) -> MessageResponse:
    check_login(settings, body.username, body.password)
    set_session_cookie(response, settings)
    return MessageResponse(message="Authenticated")


@public_router.post("/logout", response_model=MessageResponse, summary="End the admin session")
async def logout(response: Response, settings: SettingsDep) -> MessageResponse:
    clear_session_cookie(response, settings)
    return MessageResponse(message="Logged out")


# --- protected: everything else ---------------------------------------------------------------
router = APIRouter(prefix="/adminnatheel", tags=["Admin"], dependencies=[Depends(require_admin)],
                   responses=ERRORS)


def _admin_service(session: SessionDep, settings: SettingsDep, storage: StorageDep) -> AdminService:
    return AdminService(session, settings, storage)


AdminServiceDep = Annotated[AdminService, Depends(_admin_service)]


def _page(items: list, total: int, page) -> dict:
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/", summary="Dashboard overview")
async def dashboard(service: AdminServiceDep) -> dict:
    return await service.dashboard()


@router.get("/social-accounts", response_model=Page[SocialAccountRead],
            summary="Connected accounts, status and token expiration (tokens are never exposed)")
async def social_accounts(session: SessionDep, page: PaginationDep, platform: str | None = None) -> dict:
    items, total = await SocialAccountRepository(session).list(
        platform=platform.lower() if platform else None, limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.get("/posts", response_model=Page[PostRead], summary="Posts and their publication state")
async def posts(session: SessionDep, page: PaginationDep,
                status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await PostRepository(session).list(
        status=status_filter.upper() if status_filter else None, limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.get("/media", response_model=Page[MediaRead], summary="Media library")
async def media(session: SessionDep, page: PaginationDep, unattached: bool = False) -> dict:
    items, total = await MediaRepository(session).list(post_id=None, unattached=unattached,
                                                       limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.get("/publications", response_model=Page[PublicationRead], summary="Publishing history")
async def publications(session: SessionDep, page: PaginationDep, platform: str | None = None,
                       status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await PublicationRepository(session).list(
        status=status_filter.upper() if status_filter else None, platform=platform.lower() if platform else None,
        post_id=None, limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.post("/publications/{publication_id}/retry", response_model=PublicationRead,
             summary="Retry a failed publication")
async def retry_publication(publication_id: uuid.UUID, service: PublishingServiceDep):
    return await service.retry_publication(publication_id)


@router.get("/schedules", response_model=Page[ScheduleRead], summary="Scheduled posts")
async def schedules(session: SessionDep, page: PaginationDep,
                    status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await ScheduleRepository(session).list(
        status=status_filter.upper() if status_filter else None, post_id=None, limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.get("/logs", response_model=Page[PublishingLogRead], summary="Publishing / system logs")
async def logs(session: SessionDep, page: PaginationDep, platform: str | None = None, level: str | None = None,
               post_id: uuid.UUID | None = None, publication_id: uuid.UUID | None = None) -> dict:
    items, total = await PublicationRepository(session).list_logs(
        publication_id=publication_id, post_id=post_id, level=level.upper() if level else None,
        platform=platform.lower() if platform else None, limit=page.limit, offset=page.offset)
    return _page(items, total, page)


@router.get("/platform-status", summary="Per-platform configuration, capabilities and connection counts")
async def platform_status(service: AdminServiceDep) -> list[dict]:
    return await service.platform_status()


@router.get("/health", summary="System health: database, Redis, Celery workers, storage")
async def health(service: AdminServiceDep) -> dict:
    return await service.health()


# --- OAuth connection -------------------------------------------------------------------------
@router.post("/social/{platform}/connect", response_model=ConnectResponse,
             dependencies=[Depends(rate_limit("oauth", "rate_limit_oauth"))],
             summary="Begin connecting a platform (returns the official authorization URL)")
async def connect_platform(platform: str, service: OAuthServiceDep) -> ConnectResponse:
    return await service.start(parse_platform(platform))


@router.get("/social/{platform}/callback", include_in_schema=True,
            dependencies=[Depends(rate_limit("oauth", "rate_limit_oauth"))], status_code=303,
            summary="OAuth redirect target (browser redirect from the platform)",
            description="Validates the OAuth `state`, exchanges the code for tokens, encrypts and stores them, "
                        "then redirects the browser to the frontend with `?status=connected|error`.")
async def oauth_callback(platform: str, service: OAuthServiceDep, settings: SettingsDep,
                         code: str | None = None, state: str | None = None, error: str | None = None,
                         error_description: str | None = None) -> RedirectResponse:
    parsed: Platform = parse_platform(platform)
    query: dict[str, str]
    try:
        accounts = await service.complete(parsed, code=code, state=state, error=error)
        query = {"status": "connected", "platform": parsed.value, "accounts": str(len(accounts))}
    except AppError as exc:
        query = {"status": "error", "platform": parsed.value, "code": exc.code}
    target = f"{settings.frontend_url.rstrip('/')}{settings.frontend_social_accounts_path}?{urlencode(query)}"
    return RedirectResponse(target, status_code=303)


@router.delete("/social-accounts/{account_id}", response_model=SocialAccountRead,
               summary="Disconnect a social account")
async def disconnect_account(account_id: uuid.UUID, service: OAuthServiceDep):
    return await service.disconnect(account_id)
