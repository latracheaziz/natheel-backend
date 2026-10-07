"""LinkedIn (Sign In with LinkedIn using OpenID Connect + Share on LinkedIn / Posts API).

Docs: https://learn.microsoft.com/linkedin/consumer/integrations/self-serve/share-on-linkedin
Posts the content as the authenticated **member**. Posting as an Organization page requires the
Community Management API (separate product + approval) and is not implemented.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urlencode

from app.integrations.social.base import (
    AccountContext, AccountInfo, Capabilities, ProviderError, PublishContent, PublishResult,
    SocialMediaProvider, TokenSet)
from app.models.enums import Platform

_API = "https://api.linkedin.com"
_VIDEO_PART = 4 * 1024 * 1024  # LinkedIn multipart video uploads use 4 MB parts
# Characters with meaning in LinkedIn's "little text" format must be escaped in `commentary`.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def escape_commentary(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)


class LinkedInProvider(SocialMediaProvider):
    platform = Platform.LINKEDIN
    scopes = ["openid", "profile", "email", "w_member_social"]
    capabilities = Capabilities(
        supports_text=True, supports_image=True, supports_video=True, supports_multiple_images=True,
        supports_scheduling=False, supports_delete=True, max_text_length=3000, max_images=20,
        image_mime_types=("image/jpeg", "image/png"))  # WebP is not accepted by LinkedIn

    def is_configured(self) -> bool:
        return bool(self.settings.linkedin_client_id and self.settings.linkedin_client_secret.get_secret_value())

    # ------------------------------------------------------------------ OAuth
    def get_authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        params = {"response_type": "code", "client_id": self.settings.linkedin_client_id,
                  "redirect_uri": redirect_uri, "state": state, "scope": " ".join(self.scopes)}
        return f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"

    def _token_set(self, data: dict) -> TokenSet:
        return TokenSet(
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            expires_at=self._expiry(data.get("expires_in")),
            scopes=[s for s in re.split(r"[ ,]", data.get("scope", "")) if s] or list(self.scopes))

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenSet:
        async with self._client() as client:
            data = await self._json(client, "POST", "https://www.linkedin.com/oauth/v2/accessToken", data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret.get_secret_value()},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self._token_set(data)

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        # Programmatic refresh tokens are only issued to approved LinkedIn partners.
        async with self._client() as client:
            data = await self._json(client, "POST", "https://www.linkedin.com/oauth/v2/accessToken", data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret.get_secret_value()})
        tokens = self._token_set(data)
        tokens.refresh_token = tokens.refresh_token or refresh_token
        return tokens

    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        async with self._client() as client:
            me = await self._json(client, "GET", f"{_API}/v2/userinfo",
                                  headers={"Authorization": f"Bearer {tokens.access_token}"})
        return [AccountInfo(platform_account_id=str(me["sub"]), username=me.get("email"),
                            display_name=me.get("name"),
                            metadata={"type": "member", "picture": me.get("picture")})]

    async def disconnect(self, access_token: str | None, refresh_token: str | None = None) -> None:
        if not access_token:
            return
        try:
            async with self._client() as client:
                await client.post("https://www.linkedin.com/oauth/v2/revoke", data={
                    "client_id": self.settings.linkedin_client_id,
                    "client_secret": self.settings.linkedin_client_secret.get_secret_value(),
                    "token": access_token})
        except Exception:  # best effort
            return

    # ------------------------------------------------------------------ publishing helpers
    def _headers(self, ctx: AccountContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.access_token}",
                "LinkedIn-Version": self.settings.linkedin_api_version,
                "X-Restli-Protocol-Version": "2.0.0"}

    @staticmethod
    def _author(ctx: AccountContext) -> str:
        return f"urn:li:person:{ctx.platform_account_id}"

    async def _upload_image(self, client, ctx: AccountContext, item) -> str:
        init = await self._json(client, "POST", f"{_API}/rest/images?action=initializeUpload",
                                json={"initializeUploadRequest": {"owner": self._author(ctx)}},
                                headers=self._headers(ctx))
        value = init["value"]
        await self._request(client, "PUT", value["uploadUrl"], content=await item.read_bytes(),
                            headers={"Authorization": f"Bearer {ctx.access_token}",
                                     "Content-Type": "application/octet-stream"})
        return value["image"]

    async def _upload_video(self, client, ctx: AccountContext, item) -> str:
        init = await self._json(
            client, "POST", f"{_API}/rest/videos?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": self._author(ctx), "fileSizeBytes": item.size,
                                              "uploadCaptions": False, "uploadThumbnail": False}},
            headers=self._headers(ctx))
        value = init["value"]
        etags: list[str] = []
        for part in value["uploadInstructions"]:
            first, last = int(part["firstByte"]), int(part["lastByte"])
            response = await self._request(
                client, "PUT", part["uploadUrl"], content=await item.read_range(first, last - first + 1),
                headers={"Authorization": f"Bearer {ctx.access_token}", "Content-Type": "application/octet-stream"})
            etag = response.headers.get("etag")
            if not etag:
                raise ProviderError("LINKEDIN_UPLOAD_FAILED", "LinkedIn did not return an upload part ETag.",
                                    retryable=True)
            etags.append(etag.strip('"'))
        await self._request(client, "POST", f"{_API}/rest/videos?action=finalizeUpload",
                            json={"finalizeUploadRequest": {"video": value["video"],
                                                            "uploadToken": value.get("uploadToken", ""),
                                                            "uploadedPartIds": etags}},
                            headers=self._headers(ctx))
        await self._wait_video_available(client, ctx, value["video"])
        return value["video"]

    async def _wait_video_available(self, client, ctx: AccountContext, urn: str) -> None:
        for _ in range(self.settings.provider_poll_max_attempts):
            data = await self._json(client, "GET", f"{_API}/rest/videos/{quote(urn, safe='')}",
                                    headers=self._headers(ctx))
            status = data.get("status")
            if status == "AVAILABLE":
                return
            if status == "PROCESSING_FAILED":
                raise ProviderError("LINKEDIN_VIDEO_PROCESSING_FAILED", "LinkedIn failed to process the video.")
            await asyncio.sleep(self.settings.provider_poll_interval_seconds)
        raise ProviderError("LINKEDIN_VIDEO_PROCESSING_TIMEOUT", "LinkedIn is still processing the video.",
                            retryable=True)

    async def _create_post(self, client, ctx: AccountContext, text: str, media: dict | None) -> PublishResult:
        body: dict = {
            "author": self._author(ctx), "commentary": escape_commentary(text), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        if media:
            body["content"] = media
        response = await self._request(client, "POST", f"{_API}/rest/posts", json=body, headers=self._headers(ctx))
        post_urn = response.headers.get("x-restli-id")
        if not post_urn:
            raise ProviderError("LINKEDIN_BAD_RESPONSE", "LinkedIn did not return the created post id.",
                                retryable=False)
        return PublishResult(external_post_id=post_urn, url=f"https://www.linkedin.com/feed/update/{post_urn}")

    # ------------------------------------------------------------------ publishing
    async def publish_text(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        async with self._client() as client:
            return await self._create_post(client, ctx, content.text, None)

    async def publish_image(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        async with self._client(upload=True) as client:
            urns = [await self._upload_image(client, ctx, img) for img in content.images]
            media = ({"media": {"id": urns[0]}} if len(urns) == 1
                     else {"multiImage": {"images": [{"id": u, "altText": ""} for u in urns]}})
            return await self._create_post(client, ctx, content.text, media)

    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        async with self._client(upload=True) as client:
            urn = await self._upload_video(client, ctx, content.videos[0])
            return await self._create_post(client, ctx, content.text, {"media": {"id": urn}})
