"""Mailbox orchestration: connection state, token freshness, listing, reading, read/unread.

The routes stay thin and provider-agnostic; everything that knows about tokens lives here. An
access token is refreshed before a call when it is known to be near expiry, and once more
reactively if the provider rejects it anyway, which covers a token revoked mid-session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from app.core.config import get_settings
from app.core.encryption import get_cipher
from app.core.logging import get_logger
from app.emails import store
from app.emails.base import (
    MailAuthError, MailNotConfigured, MailNotConnected, MailProvider, MailProviderError, MailQuery)
from app.emails.registry import SUPPORTED, get_provider

logger = get_logger(__name__)

T = TypeVar("T")

# What the admin must still do before the page can show mail. Surfaced by `status()` so the UI
# can render real instructions instead of pretending a mailbox is attached.
STEP_CONFIGURE = "configure_credentials"
STEP_CONNECT = "connect_mailbox"
STEP_RECONNECT = "reconnect_mailbox"


def _expiry(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))


def _encryption_ready() -> bool:
    """Tokens are stored encrypted, so a missing ENCRYPTION_KEY blocks connecting at all.

    Checked up front: otherwise the admin completes the whole Google setup and only then hits an
    opaque failure at the moment the callback tries to save the tokens.
    """
    try:
        get_cipher()
    except Exception:  # ValueError for an empty key, InvalidToken for a malformed one
        return False
    return True


async def status() -> dict[str, Any]:
    """Everything the admin page needs to decide what to render, and nothing secret."""
    settings = get_settings()
    try:
        provider = get_provider()
    except ValueError as exc:
        return {
            "provider": settings.email_provider,
            "provider_label": None,
            "supported_providers": list(SUPPORTED),
            "configured": False,
            "connected": False,
            "error": str(exc),
            "next_step": STEP_CONFIGURE,
            "required_settings": [],
            "scopes": [],
        }

    encryption_ready = _encryption_ready()
    configured = provider.is_configured() and encryption_ready
    account = await store.get_account(provider.name) if configured else None
    needs_reconnect = bool(account and account.status != "connected")

    missing = list(provider.required_settings) if not provider.is_configured() else []
    if not encryption_ready:
        missing.append("ENCRYPTION_KEY")

    base = {
        "provider": provider.name,
        "provider_label": provider.label,
        "supported_providers": list(SUPPORTED),
        "configured": configured,
        "connected": bool(account) and not needs_reconnect,
        "email": account.email if account else None,
        "connected_at": account.connected_at.isoformat() if account and account.connected_at else None,
        "scopes": list(account.scopes) if account else list(provider.scopes),
        "required_settings": missing or list(provider.required_settings),
        "encryption_ready": encryption_ready,
        "supports_read_sync": provider.supports_read_sync,
        "supports_attachment_filter": provider.supports_attachment_filter,
        # Not a secret, and the admin has to register this exact string with the provider.
        "redirect_uri": settings.email_redirect_uri(),
        "error": None,
    }

    if not configured:
        base["next_step"] = STEP_CONFIGURE
        return base
    if account is None:
        base["next_step"] = STEP_CONNECT
        return base
    if needs_reconnect:
        base["next_step"] = STEP_RECONNECT
        return base

    base["next_step"] = None
    # A live profile read doubles as a credential check and gives real mailbox totals.
    try:
        profile = await _with_fresh_token(provider, account, lambda token: provider.profile(token))
    except MailAuthError:
        base.update(connected=False, next_step=STEP_RECONNECT,
                    error="انتهت صلاحية الصلاحيات الممنوحة. أعد ربط البريد.")
    except MailProviderError as exc:
        base["error"] = exc.message
    else:
        base["email"] = profile.get("email") or base["email"]
        base["mailbox"] = {"total": profile.get("total"), "unread": profile.get("unread")}
    return base


async def _require_account(provider: MailProvider):
    if not provider.is_configured():
        raise MailNotConfigured(
            "بيانات الاتصال ببريد Google غير مُعدّة على الخادم. "
            f"المطلوب: {', '.join(provider.required_settings)}."
        )
    account = await store.get_account(provider.name)
    if account is None:
        raise MailNotConnected("لم يتم ربط صندوق البريد بعد.")
    if account.status != "connected" or not account.access_token:
        raise MailAuthError("يلزم إعادة ربط صندوق البريد.")
    return account


async def _refresh(provider: MailProvider, account) -> str:
    if not account.refresh_token:
        await store.mark_status(provider.name, "reauthorize")
        raise MailAuthError("انتهت صلاحية الوصول ولا يوجد رمز تحديث. أعد ربط البريد.")
    tokens = await provider.refresh_access_token(account.refresh_token)
    await store.update_access_token(provider.name, tokens.access_token, _expiry(tokens.expires_in))
    account.access_token = tokens.access_token
    account.token_expires_at = _expiry(tokens.expires_in)
    return tokens.access_token


async def _with_fresh_token(
    provider: MailProvider, account, call: Callable[[str], Awaitable[T]]
) -> T:
    """Run `call` with a usable access token, refreshing proactively then reactively."""
    token = account.access_token
    if account.access_token_expired():
        token = await _refresh(provider, account)
    try:
        return await call(token)
    except MailAuthError:
        # The token was rejected even though it looked valid: refresh once and retry.
        token = await _refresh(provider, account)
        try:
            return await call(token)
        except MailAuthError:
            await store.mark_status(provider.name, "reauthorize")
            raise


# --------------------------------------------------------------------------- connect flow


async def start_connect() -> dict[str, Any]:
    provider = get_provider()
    if not provider.is_configured():
        raise MailNotConfigured(
            "أضف بيانات تطبيق Google (GOOGLE_CLIENT_ID و GOOGLE_CLIENT_SECRET) قبل الربط."
        )
    if not _encryption_ready():
        raise MailNotConfigured(
            "المتغير ENCRYPTION_KEY غير مُعدّ، ولا يمكن حفظ رموز الوصول مشفّرة بدونه. "
            'أنشئه بالأمر: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    settings = get_settings()
    state = await store.create_state(provider.name, settings.oauth_state_ttl_seconds)
    return {
        "provider": provider.name,
        "authorization_url": provider.authorization_url(state, settings.email_redirect_uri()),
        "expires_in": settings.oauth_state_ttl_seconds,
        "scopes": list(provider.scopes),
    }


async def complete_connect(code: str | None, state: str | None, error: str | None) -> dict[str, Any]:
    if error:
        raise MailProviderError(f"تم إلغاء الربط من Google: {error}", code="MAIL_OAUTH_DENIED")
    if not state or not code:
        raise MailProviderError("استدعاء غير صالح من Google.", code="MAIL_OAUTH_INVALID")

    provider_name = await store.consume_state(state)  # single use, whatever happens next
    if provider_name is None:
        raise MailProviderError("انتهت صلاحية طلب الربط أو تم استخدامه. ابدأ من جديد.",
                                code="MAIL_OAUTH_STATE")

    provider = get_provider(provider_name)
    settings = get_settings()
    tokens = await provider.exchange_code(code, settings.email_redirect_uri())

    granted = set(tokens.scopes or [])
    missing = [s for s in provider.scopes if s not in granted] if granted else []

    profile: dict[str, Any] = {}
    try:
        profile = await provider.profile(tokens.access_token)
    except MailProviderError:
        logger.warning("mailbox connected but the profile read failed")

    await store.save_account(
        provider.name,
        email=profile.get("email"),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=_expiry(tokens.expires_in),
        scopes=tokens.scopes or list(provider.scopes),
    )
    return {"provider": provider.name, "email": profile.get("email"), "missing_scopes": missing}


async def disconnect() -> None:
    provider = get_provider()
    account = await store.get_account(provider.name)
    if account and account.refresh_token:
        await provider.revoke(account.refresh_token)
    await store.delete_account(provider.name)


# --------------------------------------------------------------------------- mail reads


async def list_messages(
    query: MailQuery, *, page_size: int, page_token: str | None
) -> dict[str, Any]:
    provider = get_provider()
    account = await _require_account(provider)
    result = await _with_fresh_token(
        provider,
        account,
        lambda token: provider.list_messages(
            token, query, page_size=page_size, page_token=page_token,
            mailbox_email=account.email,
        ),
    )
    return {
        **result,
        "page_size": page_size,
        "email": account.email,
        "filtered": not query.is_empty(),
        "supports_read_sync": provider.supports_read_sync,
        "provider": provider.name,
    }


async def get_message(message_id: str) -> dict[str, Any] | None:
    provider = get_provider()
    account = await _require_account(provider)
    return await _with_fresh_token(
        provider,
        account,
        lambda token: provider.get_message(token, message_id, account.email),
    )


async def set_read(message_id: str, read: bool) -> dict[str, Any]:
    provider = get_provider()
    account = await _require_account(provider)
    if not provider.supports_read_sync:
        raise MailProviderError("هذا المزوّد لا يدعم تغيير حالة القراءة.",
                                code="MAIL_READ_SYNC_UNSUPPORTED")
    await _with_fresh_token(
        provider, account, lambda token: provider.set_read(token, message_id, read)
    )
    return {"id": message_id, "is_read": read}
