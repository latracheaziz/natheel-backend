"""Admin mailbox access (provider logic lives in app/emails/).

Two routers: everything is admin-authenticated except the OAuth callback, which Google reaches as
a top-level browser redirect that cannot carry the admin's bearer token. That endpoint is instead
protected by the single-use, hashed, short-lived `state` it issued, and stores tokens only for a
state it recognises.

No response from this module ever contains a token.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, Path, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.routes.reviews import require_admin_token
from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.emails import service
from app.emails.base import (
    MailAuthError, MailNotConfigured, MailNotConnected, MailProviderError, MailQuery)

logger = get_logger(__name__)


def _emails_admin(authorization: str | None = Header(default=None)) -> str:
    return require_admin_token(authorization)


router = APIRouter(prefix="/api/emails", tags=["Emails"], dependencies=[Depends(_emails_admin)])
public_router = APIRouter(prefix="/api/emails", tags=["Emails"])


class ReadState(BaseModel):
    read: bool = True


def _as_app_error(exc: MailProviderError) -> AppError:
    """Translate a provider failure into the project's error envelope, keeping the reason."""
    if isinstance(exc, MailNotConfigured):
        return ConflictError(exc.message, code="MAIL_NOT_CONFIGURED")
    if isinstance(exc, MailNotConnected):
        return ConflictError(exc.message, code="MAIL_NOT_CONNECTED")
    if isinstance(exc, MailAuthError):
        # Deliberately not 401: that is the admin's own session, and the page must not log out.
        return ConflictError(exc.message, code="MAIL_AUTH_REQUIRED")
    status = 502 if (exc.status or 0) >= 500 or exc.status is None else exc.status
    return AppError(exc.message, code=exc.code, status_code=status if status in (429, 502) else 502)


# --------------------------------------------------------------------------- connection


@router.get("/status", summary="Whether a mailbox is configured and connected, and what is missing")
async def mailbox_status() -> dict:
    return await service.status()


@router.post("/oauth/start", summary="Begin the provider's OAuth consent flow")
async def oauth_start() -> dict:
    try:
        return await service.start_connect()
    except MailProviderError as exc:
        raise _as_app_error(exc) from exc


@router.delete("/connection", summary="Disconnect the mailbox and forget its tokens")
async def oauth_disconnect() -> dict:
    try:
        await service.disconnect()
    except MailProviderError as exc:
        raise _as_app_error(exc) from exc
    return {"connected": False}


@public_router.get("/oauth/callback", include_in_schema=False,
                   summary="OAuth redirect target (public: authorised by the single-use state)")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    settings = get_settings()
    destination = f"{settings.frontend_url.rstrip('/')}{settings.frontend_emails_path}"
    try:
        await service.complete_connect(code, state, error)
    except MailProviderError as exc:
        # The reason travels as a short code, never the provider's raw payload.
        logger.warning("mailbox oauth callback failed", extra={"code": exc.code})
        return RedirectResponse(f"{destination}?email_connected=0&reason={exc.code}", status_code=303)
    return RedirectResponse(f"{destination}?email_connected=1", status_code=303)


# --------------------------------------------------------------------------- mail


@router.get("", summary="List inbox messages with search and filters")
async def list_emails(
    status: str = Query(default="all", pattern="^(all|read|unread)$"),
    sender: str | None = Query(default=None, max_length=200),
    subject: str | None = Query(default=None, max_length=200),
    q: str | None = Query(default=None, max_length=300, description="Free-text keywords"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    has_attachment: bool = Query(default=False),
    page_size: int = Query(default=25, ge=1, le=50),
    page_token: str | None = Query(default=None, max_length=512,
                                   description="Opaque cursor from the previous page"),
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise AppError("تاريخ البداية يجب أن يكون قبل تاريخ النهاية.",
                       code="INVALID_DATE_RANGE", status_code=422)
    query = MailQuery(
        status=status, sender=sender, subject=subject, keywords=q,
        date_from=date_from, date_to=date_to, has_attachment=has_attachment,
    )
    try:
        return await service.list_messages(query, page_size=page_size, page_token=page_token)
    except MailProviderError as exc:
        raise _as_app_error(exc) from exc


@router.get("/{message_id}", summary="One message in full, with a sanitised HTML body")
async def get_email(message_id: str = Path(min_length=1, max_length=200)) -> dict:
    try:
        message = await service.get_message(message_id)
    except MailProviderError as exc:
        raise _as_app_error(exc) from exc
    if message is None:
        raise NotFoundError("الرسالة غير موجودة.")
    return message


@router.post("/{message_id}/read", summary="Mark one message read or unread in the mailbox")
async def set_email_read(
    body: ReadState,
    message_id: str = Path(min_length=1, max_length=200),
) -> dict:
    try:
        return await service.set_read(message_id, body.read)
    except MailProviderError as exc:
        raise _as_app_error(exc) from exc
