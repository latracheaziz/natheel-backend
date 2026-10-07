"""Provider abstraction shared by every social integration.

`SocialMediaProvider` combines the OAuth contract (authorization URL, code exchange, refresh,
account lookup) with the publishing contract. Each provider declares `Capabilities` and only
implements the publish methods its official API genuinely supports.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import redact_text
from app.models.enums import MediaType, Platform
from app.storage.base import StorageBackend


# ---------------------------------------------------------------------------- errors
class ProviderError(Exception):
    """Failure talking to a social API. `retryable` drives Celery backoff."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status


class TokenExpiredError(ProviderError):
    def __init__(self, platform: Platform | str, message: str | None = None):
        label = str(platform).upper()
        super().__init__(f"{label}_TOKEN_EXPIRED",
                         message or f"The {str(platform).capitalize()} connection has expired. Please reconnect.",
                         retryable=False, http_status=401)


class UnsupportedCapabilityError(ProviderError):
    def __init__(self, platform: Platform | str, feature: str):
        super().__init__(f"{str(platform).upper()}_UNSUPPORTED_{feature.upper()}",
                         f"{str(platform).capitalize()} does not support {feature} via its official API.",
                         retryable=False)


# ---------------------------------------------------------------------------- data classes
@dataclass(frozen=True)
class Capabilities:
    supports_text: bool = False
    supports_image: bool = False
    supports_video: bool = False
    supports_multiple_images: bool = False
    # Native scheduling in the platform API. Our scheduler is platform independent and does not use it.
    supports_scheduling: bool = False
    supports_delete: bool = False
    max_text_length: int | None = None
    max_images: int = 1
    image_mime_types: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")
    video_mime_types: tuple[str, ...] = ("video/mp4",)
    requires_media: bool = False
    allows_mixed_media: bool = False


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # provider specific, never persisted verbatim

    def __repr__(self) -> str:  # avoid leaking tokens in tracebacks / logs
        return f"TokenSet(expires_at={self.expires_at}, scopes={self.scopes})"


@dataclass
class AccountInfo:
    platform_account_id: str
    username: str | None = None
    display_name: str | None = None
    # Some platforms issue a per-account token (Facebook Page / Instagram use Page tokens).
    token_override: TokenSet | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaItem:
    id: str
    media_type: MediaType
    mime_type: str
    size: int
    storage_key: str
    public_url: str
    storage: StorageBackend = field(repr=False, compare=False)

    def chunks(self, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        return self.storage.iter_chunks(self.storage_key, chunk_size)

    async def read_range(self, start: int, length: int) -> bytes:
        return await self.storage.read_range(self.storage_key, start, length)

    async def read_bytes(self) -> bytes:
        return await self.storage.read_bytes(self.storage_key)


@dataclass
class PublishContent:
    text: str = ""
    media: list[MediaItem] = field(default_factory=list)

    @property
    def images(self) -> list[MediaItem]:
        return [m for m in self.media if m.media_type == MediaType.IMAGE]

    @property
    def videos(self) -> list[MediaItem]:
        return [m for m in self.media if m.media_type == MediaType.VIDEO]


@dataclass
class AccountContext:
    """Decrypted credentials, handed to a provider only for the duration of one API operation."""

    account_id: str
    platform_account_id: str
    access_token: str = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    external_post_id: str
    url: str | None = None


# ---------------------------------------------------------------------------- HTTP helper
def extract_error_message(response: httpx.Response) -> tuple[str | None, str]:
    """Pulls a safe (error code, message) pair from a platform error body; never returns the raw body."""
    code: str | None = None
    message = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return code, message
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "") or None
            message = str(err.get("message") or err.get("error_user_msg") or message)
            if code is None and err.get("errors"):
                first = err["errors"][0]
                if isinstance(first, dict):
                    code = str(first.get("reason") or "") or None
        elif isinstance(err, str):
            code = err
            message = str(body.get("error_description") or err)
        elif body.get("message"):
            message = str(body["message"])
            code = str(body.get("code") or body.get("serviceErrorCode") or "") or None
    return code, redact_text(message)[:300]


class SocialMediaProvider(ABC):
    platform: Platform
    capabilities: Capabilities
    scopes: list[str] = []
    uses_pkce: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ configuration
    @abstractmethod
    def is_configured(self) -> bool:
        """True when the OAuth client credentials for this platform are present."""

    # ------------------------------------------------------------------ OAuth contract
    @abstractmethod
    def get_authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str: ...

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenSet: ...

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        raise ProviderError(f"{self.platform.value.upper()}_REFRESH_NOT_SUPPORTED",
                            f"{self.platform.value.capitalize()} tokens cannot be refreshed; reconnect the account.")

    @abstractmethod
    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        """Returns the account(s) this authorization grants access to."""

    # ------------------------------------------------------------------ lifecycle
    def connect(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        """Begin a connection: returns the official authorization URL the browser must visit."""
        return self.get_authorization_url(state, redirect_uri, code_challenge)

    async def disconnect(self, access_token: str | None, refresh_token: str | None = None) -> None:
        """Best-effort revocation at the platform. Default: nothing to revoke remotely."""
        return None

    # ------------------------------------------------------------------ content validation
    def validate_content(self, content: PublishContent) -> list[str]:
        """Return human-readable problems; empty list means the content can be published.

        Providers extend this with platform specific rules via `extra_validation`.
        """
        caps, problems = self.capabilities, []
        name = self.platform.value.capitalize()
        images, videos = content.images, content.videos
        has_text = bool(content.text.strip())

        if not has_text and not content.media:
            return [f"{name}: the post has neither text nor media."]
        if images and not caps.supports_image:
            problems.append(f"{name} does not support image posts.")
        if videos and not caps.supports_video:
            problems.append(f"{name} does not support video posts.")
        if not content.media and not caps.supports_text:
            problems.append(f"{name} does not support text-only posts; attach media.")
        if caps.requires_media and not content.media:
            problems.append(f"{name} requires media.")
        if len(images) > 1 and not caps.supports_multiple_images:
            problems.append(f"{name} supports only one image per post.")
        if len(images) > caps.max_images:
            problems.append(f"{name} supports at most {caps.max_images} images per post.")
        if len(videos) > 1:
            problems.append(f"{name} supports only one video per post.")
        if images and videos and not caps.allows_mixed_media:
            problems.append(f"{name} does not allow mixing images and videos in one post.")
        if caps.max_text_length and len(content.text) > caps.max_text_length:
            problems.append(f"{name} text is limited to {caps.max_text_length} characters "
                            f"(got {len(content.text)}).")
        for item in images:
            if item.mime_type not in caps.image_mime_types:
                problems.append(f"{name} does not accept {item.mime_type} images "
                                f"(accepted: {', '.join(caps.image_mime_types)}).")
        for item in videos:
            if item.mime_type not in caps.video_mime_types:
                problems.append(f"{name} does not accept {item.mime_type} videos.")
        problems.extend(self.extra_validation(content))
        return problems

    def extra_validation(self, content: PublishContent) -> list[str]:
        return []

    # ------------------------------------------------------------------ publishing
    async def publish_text(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        raise UnsupportedCapabilityError(self.platform, "text posts")

    async def publish_image(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        raise UnsupportedCapabilityError(self.platform, "image posts")

    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        raise UnsupportedCapabilityError(self.platform, "video posts")

    async def publish(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        """Routes to the matching capability. Callers must have run `validate_content` first."""
        problems = self.validate_content(content)
        if problems:
            raise ProviderError(f"{self.platform.value.upper()}_INVALID_CONTENT", "; ".join(problems))
        if content.videos:
            return await self.publish_video(ctx, content)
        if content.images:
            return await self.publish_image(ctx, content)
        return await self.publish_text(ctx, content)

    # ------------------------------------------------------------------ HTTP plumbing
    def _client(self, *, upload: bool = False) -> httpx.AsyncClient:
        timeout = self.settings.provider_upload_timeout_seconds if upload else self.settings.provider_http_timeout_seconds
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False)

    def _classify_http_error(self, response: httpx.Response) -> ProviderError:
        code, message = extract_error_message(response)
        status = response.status_code
        prefix = self.platform.value.upper()
        if status == 401:
            return TokenExpiredError(self.platform)
        if status == 429 or status >= 500:
            return ProviderError(f"{prefix}_{'RATE_LIMITED' if status == 429 else 'UNAVAILABLE'}", message,
                                 retryable=True, http_status=status)
        return ProviderError(f"{prefix}_{code or 'REQUEST_REJECTED'}".upper().replace(" ", "_")[:100], message,
                             retryable=False, http_status=status)

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"{self.platform.value.upper()}_NETWORK_ERROR",
                                f"Network error contacting {self.platform.value.capitalize()}: {type(exc).__name__}",
                                retryable=True) from exc
        if response.status_code >= 400:
            raise self._classify_http_error(response)
        return response

    async def _json(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(client, method, url, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.platform.value.upper()}_BAD_RESPONSE",
                                "Unexpected non-JSON response from the platform.", retryable=True) from exc
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _expiry(seconds: Any) -> datetime | None:
        try:
            return datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
        except (TypeError, ValueError):
            return None
