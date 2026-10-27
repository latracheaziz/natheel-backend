"""Social network credentials, loaded only from backend/.env (never from the frontend)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_FILE = BACKEND_ROOT / ".env"


class SocialCredentials(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=CREDENTIALS_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LinkedIn and Snapchat are the only networks NOT connected inside Zoho Social, so they
    # keep their own direct tokens here. Every other network (Facebook, Instagram, X,
    # YouTube, Pinterest, TikTok, Threads) is published exclusively through Zoho below —
    # there is no direct-API fallback for them anymore.
    linkedin_access_token: str = ""
    linkedin_author_urn: str = ""
    linkedin_api_version: str = "202506"

    snapchat_client_id: str = ""
    snapchat_client_secret: str = ""
    snapchat_refresh_token: str = ""
    snapchat_access_token: str = ""
    snapchat_profile_id: str = ""

    # Zoho Social — unified publishing + the list of channels already connected there.
    # Docs: https://www.zoho.com/social/api/introduction.html
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""
    zoho_accounts_url: str = "https://accounts.zoho.com"  # per-datacenter accounts server
    zoho_api_domain: str = "https://social.zoho.com"       # per-datacenter Social API domain
    zoho_portal_id: str = ""
    zoho_brand_id: str = ""
    # Which hub platforms are routed through Zoho instead of their direct API. Comma separated
    # Zoho network identifiers (tiktok, twitter, youtube, facebookpage, instagram, pinterest,
    # threadsbyig, linkedinpage, ...). Defaults to every network already connected in the portal.
    zoho_networks: str = "tiktok,twitter,youtube,facebookpage,instagram,pinterest,threadsbyig"
    # Optional: pin posts to one Pinterest board. Zoho requires a board_id for Pinterest; when
    # this is empty the hub asks Zoho for the brand's first board and reuses it.
    zoho_pinterest_board_id: str = ""

    def filled(self, *names: str) -> bool:
        return all(str(getattr(self, name, "")).strip() for name in names)

    def linkedin_author(self) -> str:
        author = self.linkedin_author_urn.strip()
        if author.startswith("urn:li:"):
            return author
        return f"urn:li:person:{author}"

    def zoho_ready(self) -> bool:
        return self.filled(
            "zoho_client_id", "zoho_client_secret", "zoho_refresh_token", "zoho_portal_id", "zoho_brand_id")

    def platform_ready(self) -> dict[str, bool]:
        zoho = self.zoho_ready()
        return {
            "facebook": zoho,
            "instagram": zoho,
            "linkedin": self.filled("linkedin_access_token", "linkedin_author_urn"),
            "twitter": zoho,
            "tiktok": zoho,
            "youtube": zoho,
            "pinterest": zoho,
            "threads": zoho,
            "snapchat": self.filled("snapchat_profile_id") and (
                self.filled("snapchat_access_token")
                or self.filled("snapchat_client_id", "snapchat_client_secret", "snapchat_refresh_token")
            ),
            "zoho_social": zoho,
        }


def get_social_credentials() -> SocialCredentials:
    return SocialCredentials()
