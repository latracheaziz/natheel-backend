"""Provider registry. Tests can swap providers via `override_providers`."""
from __future__ import annotations

from app.core.config import get_settings
from app.core.exceptions import UnprocessableError
from app.integrations.social.base import SocialMediaProvider
from app.integrations.social.instagram import InstagramProvider
from app.integrations.social.linkedin import LinkedInProvider
from app.integrations.social.meta import FacebookProvider
from app.integrations.social.tiktok import TikTokProvider
from app.integrations.social.youtube import YouTubeProvider
from app.models.enums import Platform

_PROVIDER_CLASSES: dict[Platform, type[SocialMediaProvider]] = {
    Platform.FACEBOOK: FacebookProvider,
    Platform.INSTAGRAM: InstagramProvider,
    Platform.LINKEDIN: LinkedInProvider,
    Platform.TIKTOK: TikTokProvider,
    Platform.YOUTUBE: YouTubeProvider,
}
_overrides: dict[Platform, SocialMediaProvider] = {}


def get_provider(platform: Platform | str) -> SocialMediaProvider:
    try:
        platform = Platform(platform)
    except ValueError as exc:
        raise UnprocessableError(f"Unknown platform '{platform}'.", code="UNKNOWN_PLATFORM") from exc
    if platform in _overrides:
        return _overrides[platform]
    return _PROVIDER_CLASSES[platform](get_settings())


def all_providers() -> list[SocialMediaProvider]:
    return [get_provider(p) for p in Platform]


def override_providers(providers: dict[Platform, SocialMediaProvider] | None) -> None:
    """Test hook: replaces (or with None, clears) provider instances."""
    _overrides.clear()
    if providers:
        _overrides.update(providers)
