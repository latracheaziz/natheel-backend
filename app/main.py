"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin.auth import require_admin
from app.admin.routes import public_router as admin_public_router
from app.admin.routes import router as admin_router
from app.api.routes import hub, media, oauth, posts, publishing, reviews, schedules, social_accounts
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger, request_id_ctx
from app.storage import get_storage

logger = get_logger(__name__)

DESCRIPTION = """
Backend for the **Social Media Publishing Platform**. There are no end-user accounts: everything is
operated by the administrator through `/adminnatheel/`.

### Authentication
Every endpoint except `/health` and `POST /adminnatheel/login` requires admin authentication, either
a signed HTTP-only session cookie (`POST /adminnatheel/login`) or HTTP Basic credentials. Credentials come
from the `ADMIN_USERNAME` / `ADMIN_PASSWORD` environment variables.

### OAuth flow (no manually-entered tokens)
1. `POST /adminnatheel/social/{platform}/connect` returns the official authorization URL (+ CSRF state).
2. The browser is redirected to the platform, the admin logs in and grants permissions.
3. The platform redirects to `GET /adminnatheel/social/{platform}/callback`.
4. The backend validates the state, exchanges the code, loads the account, **encrypts** the tokens and stores them.
5. Tokens are never returned by any endpoint.

### Errors
All errors use `{"success": false, "error": {"code": "...", "message": "..."}}`.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info("startup", extra={"environment": settings.environment})
        yield
        await dispose_engine()

    app = FastAPI(
        title="Natheel Social Publishing API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None)

    if not settings.allowed_host_list == ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization"],
        max_age=600)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid4().hex
        token = request_id_ctx.set(rid[:64])
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid[:64]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if request.url.path.startswith(("/api", "/adminnatheel")):
            response.headers["Cache-Control"] = "no-store"
        return response

    register_exception_handlers(app)

    api = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
    for module in (posts, media, social_accounts, oauth, publishing, schedules):
        api.include_router(module.router)
    app.include_router(api)
    app.include_router(hub.router)
    app.include_router(reviews.router)
    app.include_router(admin_public_router)
    app.include_router(admin_router)

    @app.get("/health", tags=["System"], summary="Liveness probe (unauthenticated, no internals)")
    async def liveness() -> dict:
        return {"status": "ok"}

    # Social platforms download uploaded media from this public path (names are unguessable UUIDs).
    storage = get_storage()
    root = getattr(storage, "root", None)
    if root is not None:
        app.mount("/media-files", StaticFiles(directory=str(root)), name="media-files")

    return app


app = create_app()
