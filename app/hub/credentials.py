"""Social network credentials, loaded only from social.env (never from the frontend)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_FILE = BACKEND_ROOT / "social.env"


class SocialCredentials(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=CREDENTIALS_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    media_public_base_url: str = ""

    meta_graph_version: str = "v21.0"
    meta_page_id: str = ""
    meta_page_access_token: str = ""
    instagram_business_account_id: str = ""

    linkedin_access_token: str = ""
    linkedin_author_urn: str = ""
    linkedin_api_version: str = "202506"

    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""

    tiktok_access_token: str = ""
    tiktok_privacy_level: str = "SELF_ONLY"

    youtube_access_token: str = ""
    youtube_refresh_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    youtube_privacy_status: str = "public"

    pinterest_access_token: str = ""
    pinterest_board_id: str = ""

    snapchat_client_id: str = ""
    snapchat_client_secret: str = ""
    snapchat_refresh_token: str = ""
    snapchat_access_token: str = ""
    snapchat_profile_id: str = ""

    def filled(self, *names: str) -> bool:
        return all(str(getattr(self, name, "")).strip() for name in names)

    def linkedin_author(self) -> str:
        author = self.linkedin_author_urn.strip()
        if author.startswith("urn:li:"):
            return author
        return f"urn:li:person:{author}"

    def platform_ready(self) -> dict[str, bool]:
        return {
            "instagram": self.filled("meta_page_access_token", "instagram_business_account_id"),
            "linkedin": self.filled("linkedin_access_token", "linkedin_author_urn"),
            "twitter": self.filled("x_api_key", "x_api_secret", "x_access_token", "x_access_token_secret"),
            "tiktok": self.filled("tiktok_access_token"),
            "youtube": self.filled("youtube_access_token") or self.filled(
                "youtube_refresh_token", "google_client_id", "google_client_secret"),
            "pinterest": self.filled("pinterest_access_token", "pinterest_board_id"),
            "snapchat": self.filled("snapchat_profile_id") and (
                self.filled("snapchat_access_token")
                or self.filled("snapchat_client_id", "snapchat_client_secret", "snapchat_refresh_token")
            ),
        }


def get_social_credentials() -> SocialCredentials:
    return SocialCredentials()
