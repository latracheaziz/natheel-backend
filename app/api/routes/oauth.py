from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import OAuthServiceDep, SettingsDep
from app.api.responses import ERRORS
from app.core.rate_limit import rate_limit
from app.integrations.social.registry import all_providers
from app.schemas.oauth import OAuthProviderInfo
from app.schemas.social_account import ConnectResponse
from app.utils.validators import parse_platform

router = APIRouter(prefix="/oauth", tags=["OAuth"], responses=ERRORS)


@router.get("/providers", response_model=list[OAuthProviderInfo], summary="OAuth providers and redirect URIs",
            description="Lists each platform's scopes, whether OAuth credentials are configured, and the exact "
                        "redirect URI to register in the platform's developer console.")
async def providers(settings: SettingsDep) -> list[OAuthProviderInfo]:
    return [OAuthProviderInfo(platform=p.platform.value, configured=p.is_configured(), scopes=p.scopes,
                              redirect_uri=settings.oauth_redirect_uri(p.platform.value)) for p in all_providers()]


@router.post("/{platform}/connect", response_model=ConnectResponse,
             dependencies=[Depends(rate_limit("oauth", "rate_limit_oauth"))], summary="Start an OAuth connection",
             description="Generates a CSRF `state` and returns the **official** authorization URL. The frontend "
                         "must redirect the browser to `authorization_url`; after consent the platform redirects to "
                         "`/adminnatheel/social/{platform}/callback`, where the code is exchanged and tokens are "
                         "stored encrypted. Tokens are never sent to the frontend.")
async def connect(platform: str, service: OAuthServiceDep) -> ConnectResponse:
    return await service.start(parse_platform(platform))
