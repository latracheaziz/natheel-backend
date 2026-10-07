"""YouTube Data API v3 via Google OAuth 2.0 (resumable video uploads).

Docs: https://developers.google.com/youtube/v3/guides/uploading_a_video
Reality check: videos uploaded by API projects that have not passed YouTube's compliance audit are
locked to `private`. Each upload costs 1,600 quota units (default quota: 10,000/day, ~6 uploads).
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlencode

from app.integrations.social.base import (
    AccountContext, AccountInfo, Capabilities, ProviderError, PublishContent, PublishResult,
    SocialMediaProvider, TokenSet)
from app.models.enums import Platform

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_REVOKE = "https://oauth2.googleapis.com/revoke"
_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class YouTubeProvider(SocialMediaProvider):
    platform = Platform.YOUTUBE
    uses_pkce = True
    scopes = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
    capabilities = Capabilities(
        supports_text=False, supports_image=False, supports_video=True, supports_scheduling=True,
        supports_delete=True, max_text_length=5000, requires_media=True)

    def is_configured(self) -> bool:
        return bool(self.settings.google_client_id and self.settings.google_client_secret.get_secret_value())

    # ------------------------------------------------------------------ OAuth
    def get_authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        params = {"client_id": self.settings.google_client_id, "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": " ".join(self.scopes), "state": state,
                  "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
        if code_challenge:
            params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
        return f"{_AUTH}?{urlencode(params)}"

    def _token_set(self, data: dict) -> TokenSet:
        return TokenSet(access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                        expires_at=self._expiry(data.get("expires_in")),
                        scopes=data.get("scope", "").split() or list(self.scopes))

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenSet:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret.get_secret_value()}
        if code_verifier:
            form["code_verifier"] = code_verifier
        async with self._client() as client:
            data = await self._json(client, "POST", _TOKEN, data=form)
        return self._token_set(data)

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        async with self._client() as client:
            data = await self._json(client, "POST", _TOKEN, data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret.get_secret_value()})
        tokens = self._token_set(data)
        tokens.refresh_token = tokens.refresh_token or refresh_token  # Google omits it on refresh
        return tokens

    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        async with self._client() as client:
            data = await self._json(client, "GET", _CHANNELS, params={"part": "snippet", "mine": "true"},
                                    headers={"Authorization": f"Bearer {tokens.access_token}"})
        items = data.get("items") or []
        if not items:
            raise ProviderError("YOUTUBE_NO_CHANNEL",
                                "The Google account has no YouTube channel. Create a channel first.")
        return [AccountInfo(platform_account_id=item["id"],
                            username=item.get("snippet", {}).get("customUrl"),
                            display_name=item.get("snippet", {}).get("title"),
                            metadata={"type": "channel"}) for item in items[:1]]

    async def disconnect(self, access_token: str | None, refresh_token: str | None = None) -> None:
        token = refresh_token or access_token
        if not token:
            return
        try:
            async with self._client() as client:
                await client.post(_REVOKE, data={"token": token})
        except Exception:  # best effort
            return

    # ------------------------------------------------------------------ publishing
    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        video = content.videos[0]
        lines = content.text.strip().splitlines() or ["Untitled"]
        title = (lines[0].strip() or "Untitled")[:100]
        metadata = {
            "snippet": {"title": title, "description": content.text[:5000], "categoryId": "22"},
            "status": {"privacyStatus": self.settings.youtube_privacy_status, "selfDeclaredMadeForKids": False}}
        auth = {"Authorization": f"Bearer {ctx.access_token}"}
        async with self._client(upload=True) as client:
            start = await self._request(
                client, "POST", _UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                json=metadata, headers={**auth, "X-Upload-Content-Type": video.mime_type,
                                        "X-Upload-Content-Length": str(video.size)})
            location = start.headers.get("location")
            if not location:
                raise ProviderError("YOUTUBE_UPLOAD_FAILED", "YouTube did not return an upload session.",
                                    retryable=True)
            data = await self._json(client, "PUT", location, content=video.chunks(2 * 1024 * 1024),
                                    headers={**auth, "Content-Type": video.mime_type,
                                             "Content-Length": str(video.size)})
        return PublishResult(external_post_id=str(data["id"]), url=f"https://www.youtube.com/watch?v={data['id']}")
