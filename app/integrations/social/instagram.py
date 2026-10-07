"""Instagram Content Publishing API (Instagram Business/Creator accounts linked to a Facebook Page).

Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing
Constraints enforced here: JPEG images only, media fetched from a public URL, no text-only posts.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.integrations.social.base import (
    AccountContext, AccountInfo, Capabilities, ProviderError, PublishContent, PublishResult, TokenSet)
from app.integrations.social.meta import MetaProviderBase
from app.models.enums import Platform


class InstagramProvider(MetaProviderBase):
    platform = Platform.INSTAGRAM
    scopes = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"]
    capabilities = Capabilities(
        supports_text=False, supports_image=True, supports_video=True, supports_multiple_images=True,
        supports_scheduling=False, supports_delete=False, max_text_length=2200, max_images=10,
        image_mime_types=("image/jpeg",), requires_media=True)

    async def get_account_info(self, tokens: TokenSet) -> list[AccountInfo]:
        accounts = []
        for page in await self._managed_pages(tokens):
            ig = page.get("instagram_business_account")
            if not ig or not page.get("access_token"):
                continue
            accounts.append(AccountInfo(
                platform_account_id=str(ig["id"]), username=ig.get("username"),
                display_name=ig.get("name") or ig.get("username"),
                token_override=TokenSet(access_token=page["access_token"], expires_at=None,
                                        scopes=list(self.scopes)),
                metadata={"type": "instagram_business", "facebook_page_id": str(page["id"])}))
        if not accounts:
            raise ProviderError(
                "INSTAGRAM_NO_BUSINESS_ACCOUNT",
                "No Instagram Business/Creator account linked to a Facebook Page was found. Convert the "
                "Instagram account to Professional and link it to a Page you manage.")
        return accounts

    # ------------------------------------------------------------------ helpers
    async def _create_container(self, client, ctx: AccountContext, params: dict[str, Any]) -> str:
        data = await self._json(client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/media",
                                data=params, headers=self._auth(ctx))
        return str(data["id"])

    async def _wait_until_ready(self, client, ctx: AccountContext, container_id: str) -> None:
        for _ in range(self.settings.provider_poll_max_attempts):
            data = await self._json(client, "GET", f"{self.graph_url}/{container_id}",
                                    params={"fields": "status_code,status"}, headers=self._auth(ctx))
            status = data.get("status_code")
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise ProviderError("INSTAGRAM_MEDIA_PROCESSING_FAILED",
                                    f"Instagram could not process the media ({data.get('status', status)}).")
            await asyncio.sleep(self.settings.provider_poll_interval_seconds)
        raise ProviderError("INSTAGRAM_MEDIA_PROCESSING_TIMEOUT",
                            "Instagram is still processing the media.", retryable=True)

    async def _publish_container(self, client, ctx: AccountContext, container_id: str) -> PublishResult:
        data = await self._json(client, "POST", f"{self.graph_url}/{ctx.platform_account_id}/media_publish",
                                data={"creation_id": container_id}, headers=self._auth(ctx))
        return PublishResult(external_post_id=str(data["id"]))

    # ------------------------------------------------------------------ publishing
    async def publish_image(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        images = content.images
        async with self._client() as client:
            if len(images) == 1:
                container = await self._create_container(
                    client, ctx, {"image_url": images[0].public_url, "caption": content.text})
            else:
                children = []
                for image in images:
                    child = await self._create_container(
                        client, ctx, {"image_url": image.public_url, "is_carousel_item": "true"})
                    await self._wait_until_ready(client, ctx, child)
                    children.append(child)
                container = await self._create_container(
                    client, ctx, {"media_type": "CAROUSEL", "children": ",".join(children),
                                  "caption": content.text})
            await self._wait_until_ready(client, ctx, container)
            return await self._publish_container(client, ctx, container)

    async def publish_video(self, ctx: AccountContext, content: PublishContent) -> PublishResult:
        # Feed videos are published as Reels (the only video type the API still accepts).
        async with self._client() as client:
            container = await self._create_container(
                client, ctx, {"media_type": "REELS", "video_url": content.videos[0].public_url,
                              "caption": content.text})
            await self._wait_until_ready(client, ctx, container)
            return await self._publish_container(client, ctx, container)
