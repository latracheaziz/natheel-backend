from __future__ import annotations

from app.core.exceptions import AppError
from app.integrations.social.base import ProviderError, TokenExpiredError


def provider_error_to_app_error(exc: ProviderError) -> AppError:
    """Maps an upstream failure to an API error without leaking raw platform responses."""
    if isinstance(exc, TokenExpiredError):
        return AppError(exc.message, code=exc.code, status_code=409)
    return AppError(exc.message, code=exc.code, status_code=503 if exc.retryable else 502)
