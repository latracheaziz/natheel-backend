"""Admin authentication: signed HTTP-only session cookie OR HTTP Basic, credentials from environment."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import APIKeyCookie, HTTPBasic, HTTPBasicCredentials

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, AuthenticationError, RateLimitedError
from app.core.logging import get_logger
from app.core.rate_limit import client_ip, get_rate_limit_backend, parse_limit
from app.core.security import create_session_token, verify_admin_credentials, verify_session_token

logger = get_logger(__name__)

http_basic = HTTPBasic(auto_error=False, description="Admin credentials (ADMIN_USERNAME / ADMIN_PASSWORD).")
session_cookie = APIKeyCookie(name=get_settings().admin_cookie_name, auto_error=False,
                              description="Session cookie issued by POST /adminnatheel/login.")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _trusted_origins(settings: Settings) -> set[str]:
    return {*settings.cors_origin_list, settings.backend_public_url.rstrip("/")}


def _enforce_origin(request: Request, settings: Settings) -> None:
    """CSRF defence in depth for credentials browsers attach automatically (cookie / cached Basic)."""
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in _trusted_origins(settings):
        raise AppError("Cross-origin request rejected.", code="ORIGIN_NOT_ALLOWED", status_code=403)


async def _register_failed_attempt(request: Request, settings: Settings) -> None:
    limit, window = parse_limit(settings.rate_limit_login)
    try:
        count, retry_after = await get_rate_limit_backend().hit(f"ratelimit:adminauth:{client_ip(request)}", window)
    except Exception:
        return
    if count > limit:
        raise RateLimitedError("Too many failed authentication attempts.", headers={"Retry-After": str(retry_after)})


async def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    basic: Annotated[HTTPBasicCredentials | None, Depends(http_basic)] = None,
    cookie: Annotated[str | None, Depends(session_cookie)] = None,
) -> str:
    if cookie and verify_session_token(settings, cookie):
        _enforce_origin(request, settings)
        return "admin"
    if basic is not None:
        if verify_admin_credentials(settings, basic.username, basic.password):
            _enforce_origin(request, settings)
            return "admin"
        await _register_failed_attempt(request, settings)
    headers = {"WWW-Authenticate": 'Basic realm="adminnatheel", charset="UTF-8"'} \
        if "text/html" in request.headers.get("accept", "") or basic is not None else None
    raise AuthenticationError("Admin authentication required.", headers=headers)


AdminDep = Annotated[str, Depends(require_admin)]


def set_session_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        settings.admin_cookie_name, create_session_token(settings), max_age=settings.admin_session_ttl_seconds,
        httponly=True, secure=settings.cookie_secure, samesite=settings.admin_cookie_samesite, path="/")


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.admin_cookie_name, path="/", secure=settings.cookie_secure,
                           httponly=True, samesite=settings.admin_cookie_samesite)


def check_login(settings: Settings, username: str, password: str) -> None:
    if not verify_admin_credentials(settings, username, password):
        logger.warning("admin_login_failed")  # never log the submitted credentials
        raise AuthenticationError("Invalid credentials.", code="INVALID_CREDENTIALS")
