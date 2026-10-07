"""Application settings, loaded from environment variables / .env (never hardcoded)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder values shipped in .env.example. Production refuses to boot with them.
_INSECURE_MARKERS = ("change-me", "changeme", "replace-me", "password", "secret")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General -----------------------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    docs_enabled: bool = True
    allowed_hosts: str = "*"  # comma separated; restrict in production

    # --- Infrastructure ----------------------------------------------------------------
    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db
    redis_url: str = "redis://localhost:6379/0"

    # --- Admin access ------------------------------------------------------------------
    admin_username: str
    admin_password: SecretStr
    admin_session_secret: SecretStr
    admin_session_ttl_seconds: int = 8 * 3600
    admin_cookie_name: str = "natheel_admin_session"
    admin_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    admin_cookie_secure: bool | None = None  # None => True in production

    # --- Encryption --------------------------------------------------------------------
    # One Fernet key, or several comma separated keys (first encrypts, all decrypt: rotation).
    encryption_key: SecretStr

    # --- URLs / CORS -------------------------------------------------------------------
    frontend_url: str = "http://localhost:5173"
    cors_origins: str = ""  # extra comma separated origins besides FRONTEND_URL
    backend_public_url: str = "http://localhost:8000"  # used to build OAuth redirect URIs
    frontend_social_accounts_path: str = "/adminnatheel/social-accounts"
    media_public_base_url: str | None = None  # must be publicly reachable by social platforms

    # --- Storage -----------------------------------------------------------------------
    storage_backend: Literal["local"] = "local"
    storage_local_path: str = "./storage"
    max_image_bytes: int = 10 * 1024 * 1024
    max_video_bytes: int = 512 * 1024 * 1024
    image_min_dimension: int = 100
    image_max_dimension: int = 8192

    # --- OAuth apps --------------------------------------------------------------------
    meta_client_id: str = ""
    meta_client_secret: SecretStr = SecretStr("")
    meta_graph_version: str = "v21.0"
    linkedin_client_id: str = ""
    linkedin_client_secret: SecretStr = SecretStr("")
    linkedin_api_version: str = "202506"  # YYYYMM, must be an active LinkedIn API version
    tiktok_client_key: str = ""
    tiktok_client_secret: SecretStr = SecretStr("")
    tiktok_privacy_level: str = "SELF_ONLY"  # unaudited TikTok apps may only post privately
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    youtube_privacy_status: Literal["private", "unlisted", "public"] = "private"
    oauth_state_ttl_seconds: int = 600

    # --- Publishing --------------------------------------------------------------------
    publish_max_retries: int = 5
    publish_backoff_base_seconds: int = 30
    publish_backoff_max_seconds: int = 3600
    provider_http_timeout_seconds: float = 30.0
    provider_upload_timeout_seconds: float = 900.0
    provider_poll_interval_seconds: float = 5.0
    provider_poll_max_attempts: int = 120
    stale_publication_minutes: int = 60
    token_refresh_leeway_seconds: int = 300

    # --- Rate limiting -----------------------------------------------------------------
    rate_limit_backend: Literal["redis", "memory"] = "redis"
    rate_limit_login: str = "5/60"  # "<requests>/<window seconds>"
    rate_limit_oauth: str = "20/60"
    rate_limit_publish: str = "30/60"
    rate_limit_upload: str = "60/60"

    # --- Derived helpers ---------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production if self.admin_cookie_secure is None else self.admin_cookie_secure

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [self.frontend_url, *self.cors_origins.split(",")]
        cleaned = {o.strip().rstrip("/") for o in origins if o.strip()}
        # localhost and 127.0.0.1 are different origins to the browser.
        aliases = set()
        for origin in cleaned:
            if "://localhost" in origin:
                aliases.add(origin.replace("://localhost", "://127.0.0.1", 1))
            elif "://127.0.0.1" in origin:
                aliases.add(origin.replace("://127.0.0.1", "://localhost", 1))
        return sorted(cleaned | aliases)

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def public_media_base(self) -> str:
        return (self.media_public_base_url or f"{self.backend_public_url.rstrip('/')}/media-files").rstrip("/")

    def oauth_redirect_uri(self, platform: str) -> str:
        return f"{self.backend_public_url.rstrip('/')}/adminnatheel/social/{platform}/callback"

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if not self.is_production:
            return self
        problems: list[str] = []
        if "*" in self.cors_origin_list:
            problems.append("CORS origins must not contain '*'")
        if "*" in self.allowed_host_list:
            problems.append("ALLOWED_HOSTS must be restricted")
        for name, value in (
            ("ADMIN_PASSWORD", self.admin_password.get_secret_value()),
            ("ADMIN_SESSION_SECRET", self.admin_session_secret.get_secret_value()),
        ):
            if len(value) < 16 or any(m in value.lower() for m in _INSECURE_MARKERS):
                problems.append(f"{name} must be a strong random value (>=16 chars)")
        if not self.backend_public_url.startswith("https://"):
            problems.append("BACKEND_PUBLIC_URL must use https")
        if not self.frontend_url.startswith("https://"):
            problems.append("FRONTEND_URL must use https")
        if not self.cookie_secure:
            problems.append("ADMIN_COOKIE_SECURE must be enabled")
        if self.rate_limit_backend != "redis":
            problems.append("RATE_LIMIT_BACKEND must be redis")
        if problems:
            raise ValueError("Insecure production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
