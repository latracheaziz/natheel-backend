"""Meta (Facebook Login) shared OAuth + Graph API plumbing, and the Facebook Pages provider.

Docs: https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow
      https://developers.facebook.com/docs/pages-api/posts
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from app.integrations.social.base import (
    AccountContext, AccountInfo, Capabilities, ProviderError, PublishContent, PublishResult,
    SocialMediaProvider, TokenExpiredError, TokenSet, extract_error_message)
from app.models.enums import Platform

# Graph API error codes: https://developers.facebook.com/docs/graph-api/guides/error-handling
_TOKEN_CODES = {"190", "102", "458", "459", "460", "463", "467", "492"}
_RETRYABLE_CODES = {"1", "2", "4", "17", "32", "341", "368", "613"}


class MetaProviderBase(SocialMediaProvider):
    """OAuth is identical for Facebook and Instagram (both use Facebook Login for Business)."""

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.settings.meta_graph_version}"

    def is_configured(self) -> bool:
        return bool(self.settings.meta_client_id and self.settings.meta_client_secret.get_secret_value())

    # ------------------------------------------------------------------ OAuth
    def get_authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        params = {
            "client_id": self.settings.meta_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(self.scopes),
        }
        return (f"https://www.facebook.com/{self.settings.meta_graph_version}/dialog/oauth?"
                f"{urlencode(params)}")

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenSet:
        secret = self.settings.meta_client_secret.get_secret_value()
        async with self._client() as client:
            short = await self._json(client, "POST", f"{self.graph_url}/oauth/access_token", data={
                "client_id": self.settings.meta_client_id, "client_secret": secret,
                "redirect_uri": redirect_uri, "code": code})
            # Upgrade to a long-lived (~60 day) user token; Page tokens derived from it never expire.
            long = await self._json(client, "POST", f"{self.graph_url}/oauth/access_token", data={
                "grant_type": "fb_exchange_token", "client_id": self.settings.meta_client_id,
                "client_secret": secret, "fb_exchange_token": short["access_token"]})
        return TokenSet(access_token=long["access_token"], expires_at=self._expiry(long.get("expires_in")),
                        scopes=list(self.scopes))

    async def _managed_pages(self, tokens: TokenSet) -> list[dict[str, Any]]:
        fields = "id,name,username,access_token,tasks,instagram_business_account{id,username,name}"
        async with self._client() as client:
            data = await self._json(client, "GET", f"{self.graph_url}/me/accounts",
                                    params={"fields": fields, "limit": 100},
                                    headers={"Authorization": f"Bearer {tokens.access_token}"})
        return list(data.get("data", []))

    # ------------------------------------------------------------------ errors
    def _classify_http_error(self, response: httpx.Response) -> ProviderError:
        try:
            err = response.json().get("error", {})
            code = str(err.get("code", ""))
        except (ValueError, AttributeError):
            return super()._classify_http_error(response)
        _, message = extract_error_message(response)
        prefix = self.platform.value.upper()
        if code in _TOKEN_CODES or response.status_code == 401:
            return TokenExpiredError(self.platform)
        if code in _RETRYABLE_CODES or response.status_code >= 500:
            return ProviderError(f"{prefix}_GRAPH_{code or 'UNAVAILABLE'}", message, retryable=True,
                                 http_status=response.status_code)
        return ProviderError(f"{prefix}_GRAPH_{code or 'REJECTED'}", message, retryable=False,
                             http_status=response.status_code)

    def _auth(self, ctx: AccountContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.access_token}"}


class FacebookProvider(MetaProviderBase):
    """Publishes to Facebook **Pages** (the Graph API cannot post to personal profiles).

    Requires an app with `pages_manage_posts` (App Review for non-admin/tester users) and an
    admin who has a role on the Page.
    """

    platform = Platform.FACEBOOK
    scopes = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
    capabilities = Capabilities(
        supports_text=True, supports_image=True, supports_video=True, supports_multiple_images=True,
        supports_scheduling=True, supports_delete=True, max_text_length=63_206, max_images=10)

    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        accounts = []
        for page in await self._managed_pages(tokens):
            if not page.get("access_token"):
                continue
            accounts.append(AccountInfo(
                platform_account_id=str(page["id"]), username=page.get("username"),
                display_name=page.get("name"),
                token_override=TokenSet(access_token=page["access_token"], expires_at=None,
                                        scopes=list(self.scopes)),
                metadata={"type": "page", "tasks": page.get("tasks", [])}))
        if not accounts:
            raise ProviderError("FACEBOOK_NO_PAGES",
                                "No Facebook Pages with publishing access were granted. Select a Page in the "
                                "authorization dialog.")
        return accounts

    async def publish_text(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        async with self._client() as client:
            data = await self._json(client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/feed",
                                    data={"message": content.text}, headers=self._auth(ctx))
        return PublishResult(external_post_id=str(data["id"]))

    async def publish_image(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        images = content.images
        async with self._client() as client:
            if len(images) == 1:
                data = await self._json(
                    client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/photos",
                    data={"url": images[0].public_url, "caption": content.text, "published": "true"},
                    headers=self._auth(ctx))
                return PublishResult(external_post_id=str(data.get("post_id") or data["id"]))
            # Multi-photo post: upload unpublished photos, then attach them to a feed post.
            form: dict[str, str] = {"message": content.text}
            for index, image in enumerate(images):
                photo = await self._json(
                    client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/photos",
                    data={"url": image.public_url, "published": "false"}, headers=self._auth(ctx))
                form[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo["id"]})
            data = await self._json(client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/feed",
                                    data=form, headers=self._auth(ctx))
        return PublishResult(external_post_id=str(data["id"]))

    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        video = content.videos[0]
        url = f"https://graph-video.facebook.com/{self.settings.meta_graph_version}/{ctx.platform_account_id}/videos"
        async with self._client(upload=True) as client:
            data = await self._json(client, "POST", url,
                                    data={"file_url": video.public_url, "description": content.text},
                                    headers=self._auth(ctx))
        return PublishResult(external_post_id=str(data["id"]))
