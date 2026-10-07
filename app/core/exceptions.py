"""Domain exceptions and centralized exception handlers.

Every error leaves the API in the same envelope:

    {"success": false, "error": {"code": "...", "message": "...", "details": ...}}
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "APP_ERROR"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 details: Any = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        self.headers = headers


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class UnprocessableError(AppError):
    status_code = 422
    code = "UNPROCESSABLE"


class AuthenticationError(AppError):
    status_code = 401
    code = "NOT_AUTHENTICATED"


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"


class InvalidStateTransition(ConflictError):
    code = "INVALID_STATE_TRANSITION"


class OAuthStateError(AppError):
    status_code = 400
    code = "OAUTH_STATE_INVALID"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"success": False, "error": error}


_HTTP_CODES = {
    400: "BAD_REQUEST", 401: "NOT_AUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE", 429: "RATE_LIMITED",
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, exc.details),
                            status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(error_body(code, message), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo submitted values back (they may contain secrets).
        details = [{"loc": [str(p) for p in e.get("loc", ())], "message": e.get("msg", "invalid")}
                   for e in exc.errors()]
        return JSONResponse(error_body("VALIDATION_ERROR", "Request validation failed.", details),
                            status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", extra={"path": request.url.path, "error_type": type(exc).__name__},
                     exc_info=exc)
        return JSONResponse(error_body("INTERNAL_SERVER_ERROR", "An unexpected error occurred."),
                            status_code=500)
