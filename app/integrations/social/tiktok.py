"""TikTok Login Kit + Content Posting API (Direct Post, FILE_UPLOAD source).

Docs: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
Reality check: until TikTok **audits** your app, every post is forced to private (SELF_ONLY) and the
target account must be a registered test user. Photo posts via the API only support PULL_FROM_URL from
a TikTok-verified domain, so images are not offered here.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode

from app.integrations.social.base import (
    AccountContext, AccountInfo, Capabilities, ProviderError, PublishContent, PublishResult,
    SocialMediaProvider, TokenExpiredError, TokenSet)
from app.models.enums import Platform

_API = "https://open.tiktokapis.com"
_CHUNK = 10 * 1024 * 1024
_MIN_SINGLE_CHUNK = 5 * 1024 * 1024


class TikTokProvider(SocialMediaProvider):
    platform = Platform.TIKTOK
    scopes = ["user.info.basic", "video.publish"]
    capabilities = Capabilities(
        supports_text=False, supports_image=False, supports_video=True, supports_multiple_images=False,
        supports_scheduling=False, supports_delete=False, max_text_length=2200, requires_media=True)

    def is_configured(self) -> bool:
        return bool(self.settings.tiktok_client_key and self.settings.tiktok_client_secret.get_secret_value())

    # ------------------------------------------------------------------ OAuth
    def get_authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        params = {"client_key": self.settings.tiktok_client_key, "scope": ",".join(self.scopes),
                  "response_type": "code", "redirect_uri": redirect_uri, "state": state}
        return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"

    def _token_set(self, data: dict) -> TokenSet:
        if "access_token" not in data:
            raise ProviderError("TIKTOK_TOKEN_REQUEST_FAILED",
                                str(data.get("error_description") or "TikTok rejected the token request."))
        return TokenSet(
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            expires_at=self._expiry(data.get("expires_in")),
            scopes=[s for s in re.split(r"[ ,]", data.get("scope", "")) if s] or list(self.scopes),
            extra={"open_id": data.get("open_id")})

    async def _token_request(self, form: dict) -> TokenSet:
        form = {**form, "client_key": self.settings.tiktok_client_key,
                "client_secret": self.settings.tiktok_client_secret.get_secret_value()}
        async with self._client() as client:
            data = await self._json(client, "POST", f"{_API}/v2/oauth/token/", data=form,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self._token_set(data)

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenSet:
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": redirect_uri})

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        tokens = await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        tokens.refresh_token = tokens.refresh_token or refresh_token
        return tokens

    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        async with self._client() as client:
            data = await self._json(client, "GET", f"{_API}/v2/user/info/",
                                    params={"fields": "open_id,union_id,avatar_url,display_name"},
                                    headers={"Authorization": f"Bearer {tokens.access_token}"})
        self._raise_for_api_error(data)
        user = data.get("data", {}).get("user", {})
        open_id = user.get("open_id") or tokens.extra.get("open_id")
        if not open_id:
            raise ProviderError("TIKTOK_NO_ACCOUNT", "TikTok did not return the account identity.")
        return [AccountInfo(platform_account_id=str(open_id), username=user.get("display_name"),
                            display_name=user.get("display_name"),
                            metadata={"type": "creator", "avatar_url": user.get("avatar_url")})]

    async def disconnect(self, access_token: str | None, refresh_token: str | None = None) -> None:
        if not access_token:
            return
        try:
            async with self._client() as client:
                await client.post(f"{_API}/v2/oauth/revoke/", data={
                    "client_key": self.settings.tiktok_client_key,
                    "client_secret": self.settings.tiktok_client_secret.get_secret_value(),
                    "token": access_token})
        except Exception:  # best effort
            return

    # ------------------------------------------------------------------ publishing
    def _raise_for_api_error(self, data: dict) -> None:
        error = data.get("error") or {}
        code = error.get("code", "ok")
        if code == "ok":
            return
        message = str(error.get("message") or "TikTok rejected the request.")[:300]
        if code in {"access_token_invalid", "access_token_expired", "scope_not_authorized"}:
            raise TokenExpiredError(self.platform, message)
        retryable = code in {"rate_limit_exceeded", "internal_error"}
        raise ProviderError(f"TIKTOK_{code}".upper(), message, retryable=retryable)

    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        video = content.videos[0]
        auth = {"Authorization": f"Bearer {ctx.access_token}"}
        json_headers = {**auth, "Content-Type": "application/json; charset=UTF-8"}
        async with self._client(upload=True) as client:
            creator = await self._json(client, "POST", f"{_API}/v2/post/publish/creator_info/query/",
                                       headers=json_headers)
            self._raise_for_api_error(creator)
            options = creator.get("data", {}).get("privacy_level_options", [])
            wanted = self.settings.tiktok_privacy_level
            privacy = wanted if wanted in options else (options[0] if options else wanted)

            if video.size <= _MIN_SINGLE_CHUNK:
                chunk_size, total_chunks = video.size, 1
            else:
                chunk_size, total_chunks = _CHUNK, video.size // _CHUNK  # last chunk absorbs the remainder
            init = await self._json(client, "POST", f"{_API}/v2/post/publish/video/init/", headers=json_headers, json={
                "post_info": {"title": content.text[:2200], "privacy_level": privacy, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False},
                "source_info": {"source": "FILE_UPLOAD", "video_size": video.size,
                                "chunk_size": chunk_size, "total_chunk_count": total_chunks}})
            self._raise_for_api_error(init)
            publish_id = init["data"]["publish_id"]
            upload_url = init["data"]["upload_url"]

            for index in range(total_chunks):
                start = index * chunk_size
                end = video.size - 1 if index == total_chunks - 1 else start + chunk_size - 1
                await self._request(client, "PUT", upload_url, content=await video.read_range(start, end - start + 1),
                                    headers={"Content-Type": video.mime_type,
                                             "Content-Range": f"bytes {start}-{end}/{video.size}"})
            return await self._wait_for_publish(client, json_headers, publish_id)

    async def _wait_for_publish(self, client, headers: dict, publish_id: str) -> PublishResult:
        for _ in range(self.settings.provider_poll_max_attempts):
            data = await self._json(client, "POST", f"{_API}/v2/post/publish/status/fetch/", headers=headers,
                                    json={"publish_id": publish_id})
            self._raise_for_api_error(data)
            info = data.get("data", {})
            status = info.get("status")
            if status == "PUBLISH_COMPLETE":
                ids = info.get("publicaly_available_post_id") or []  # (sic) spelling used by TikTok's API
                return PublishResult(external_post_id=str(ids[0]) if ids else publish_id)
            if status == "FAILED":
                raise ProviderError(f"TIKTOK_{info.get('fail_reason', 'PUBLISH_FAILED')}".upper()[:100],
                                    f"TikTok failed to publish the video ({info.get('fail_reason', 'unknown')}).")
            await asyncio.sleep(self.settings.provider_poll_interval_seconds)
        raise ProviderError("TIKTOK_PUBLISH_TIMEOUT", "TikTok is still processing the video.", retryable=True)
