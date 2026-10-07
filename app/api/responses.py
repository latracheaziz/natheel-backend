from __future__ import annotations

from app.schemas.common import ErrorResponse

ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Admin authentication required (NOT_AUTHENTICATED)."},
    404: {"model": ErrorResponse, "description": "Resource not found (e.g. POST_NOT_FOUND)."},
    409: {"model": ErrorResponse, "description": "State conflict (e.g. INSTAGRAM_TOKEN_EXPIRED, "
                                                  "POST_NOT_EDITABLE, ALREADY_PUBLISHED)."},
    422: {"model": ErrorResponse, "description": "Validation failed (VALIDATION_ERROR, CONTENT_VALIDATION_FAILED)."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded (RATE_LIMITED)."},
}
