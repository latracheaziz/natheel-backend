"""Live, nullable social analytics. No synthetic values are produced.

Facebook, Instagram, X, YouTube, Pinterest, TikTok, and Threads are connected through Zoho
Social, which does not expose follower counts in its API — only connection status, name, and
avatar per channel. Only LinkedIn and Snapchat (not Zoho-managed) still report real follower
counts, fetched directly from their own APIs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.hub import zoho_social
from app.hub.credentials import SocialCredentials, get_social_credentials
from app.hub.publish import _error_text, _snapchat_bearer
from app.hub.store import list_posts

PLATFORM_NAMES = {
    "tiktok": "تيك توك",
    "snapchat": "سناب شات",
    "instagram": "إنستغرام",
    "linkedin": "لينكد إن",
    "twitter": "إكس",
    "pinterest": "بنترست",
    "youtube": "يوتيوب",
    "facebook": "فيسبوك",
    "threads": "ثريدز",
}


def _empty(platform: str, reason: str, *, connected: bool = False) -> dict:
    return {
        "platform": platform,
        "name": PLATFORM_NAMES[platform],
        "connected": connected,
        "available": False,
        "reason": reason,
        "username": None,
        "followers_count": None,
        "following_count": None,
        "posts_count": None,
        "likes_count": None,
        "views_count": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw": None,
    }


def _integer(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ready(platform: str, data: dict, raw: dict) -> dict:
    return {
        "platform": platform,
        "name": PLATFORM_NAMES[platform],
        "connected": True,
        "available": True,
        "reason": None,
        "username": data.get("username"),
        "followers_count": _integer(data.get("followers_count")),
        "following_count": _integer(data.get("following_count")),
        "posts_count": _integer(data.get("posts_count")),
        "likes_count": _integer(data.get("likes_count")),
        "views_count": _integer(data.get("views_count")),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw,
    }


async def _zoho_channels_safe(client: httpx.AsyncClient, c: SocialCredentials) -> list[dict]:
    if not c.zoho_ready():
        return []
    try:
        return await zoho_social.get_channels(client, c)
    except Exception:
        return []


def _zoho_account_entry(platform: str, channel: dict) -> dict:
    connected = channel.get("status") == "token_active"
    return {
        "platform": platform,
        "name": PLATFORM_NAMES[platform],
        "connected": connected,
        "available": False,
        "reason": (
            "عدد المتابعين غير متوفر عبر واجهة Zoho Social الحالية (تعرض الاسم والحالة فقط)."
            if connected else f"الحالة في Zoho Social: {channel.get('status') or 'غير معروفة'}"
        ),
        "username": channel.get("name"),
        "followers_count": None,
        "following_count": None,
        "posts_count": None,
        "likes_count": None,
        "views_count": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw": channel,
    }







async def _linkedin(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("linkedin_access_token", "linkedin_author_urn"):
        return _empty("linkedin", "بيانات اعتماد لينكد إن غير مكتملة.")
    author = c.linkedin_author()
    headers = {
        "Authorization": f"Bearer {c.linkedin_access_token.strip()}",
        "LinkedIn-Version": c.linkedin_api_version.strip() or "202506",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    if not author.startswith("urn:li:organization:"):
        return _empty(
            "linkedin",
            "واجهة لينكد إن لا توفر عدد متابعي الحساب الشخصي ضمن صلاحية النشر الحالية.",
            connected=True,
        )
    response = await client.get(
        "https://api.linkedin.com/rest/organizationalEntityFollowerStatistics",
        params={"q": "organizationalEntity", "organizationalEntity": author},
        headers=headers,
    )
    if response.status_code >= 400:
        return _empty("linkedin", _error_text(response), connected=True)
    body = response.json()
    element = next(iter(body.get("elements") or []), {})
    counts = element.get("followerCounts") or {}
    total = sum(value for value in counts.values() if isinstance(value, int)) or None
    return _ready("linkedin", {
        "username": author,
        "followers_count": total,
    }, body)


async def _snapchat(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("snapchat_profile_id"):
        return _empty("snapchat", "SNAPCHAT_PROFILE_ID غير موجود.")
    token, error = await _snapchat_bearer(c, client)
    if not token:
        return _empty("snapchat", error or "تعذر إنشاء رمز سناب شات.", connected=True)
    response = await client.get(
        f"https://businessapi.snapchat.com/v1/public_profiles/{c.snapchat_profile_id.strip()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code >= 400:
        return _empty("snapchat", _error_text(response), connected=True)
    body = response.json()
    wrapper = next(iter(body.get("public_profiles") or []), {})
    profile = wrapper.get("public_profile") or body.get("public_profile") or body
    return _ready("snapchat", {
        "username": profile.get("display_name"),
        "followers_count": profile.get("subscriber_count") or profile.get("subscribers_count"),
        "posts_count": profile.get("story_count"),
    }, body)


async def _account_metrics(c: SocialCredentials) -> list[dict]:
    timeout = httpx.Timeout(30.0, read=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        channels, snapchat_metric, linkedin_metric = await asyncio.gather(
            _zoho_channels_safe(client, c),
            _snapchat(client, c),
            _linkedin(client, c),
            return_exceptions=True,
        )
    if isinstance(channels, Exception):
        channels = []
    channel_by_network = {item.get("network"): item for item in channels if item.get("network")}
    metrics: list[dict] = [
        linkedin_metric if not isinstance(linkedin_metric, Exception) else _empty("linkedin", str(linkedin_metric)),
        snapchat_metric if not isinstance(snapchat_metric, Exception) else _empty("snapchat", str(snapchat_metric)),
    ]
    for platform, network in zoho_social.NETWORK_MAP.items():
        channel = channel_by_network.get(network)
        if channel is not None:
            metrics.append(_zoho_account_entry(platform, channel))
        elif c.zoho_ready():
            metrics.append(_empty(platform, "غير متصل داخل Zoho Social حالياً."))
        else:
            metrics.append(_empty(platform, "Zoho Social غير مُهيّأ في .env."))
    return metrics


async def _post_metric(client: httpx.AsyncClient, c: SocialCredentials, post: dict, result: dict) -> dict:
    platform = str(result.get("platform") or "")
    external_id = str(result.get("external_id") or "")
    base = {
        "hub_post_id": str(post.get("id") or ""),
        "platform": platform,
        "external_post_id": external_id or None,
        "available": False,
        "reason": None,
        "likes_count": None,
        "comments_count": None,
        "shares_count": None,
        "views_count": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw": None,
    }
    if result.get("status") != "published" or not external_id:
        base["reason"] = "لا يوجد معرّف منشور منشور على المنصة."
        return base
    response: httpx.Response | None = None
    if platform == "linkedin" and c.filled("linkedin_access_token"):
        response = await client.get(
            f"https://api.linkedin.com/rest/socialActions/{quote(external_id, safe='')}",
            headers={
                "Authorization": f"Bearer {c.linkedin_access_token.strip()}",
                "LinkedIn-Version": c.linkedin_api_version.strip() or "202506",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )
    else:
        # Facebook, Instagram, X, YouTube, Pinterest, TikTok, and Threads are published
        # through Zoho; their live engagement numbers come from the Zoho-sourced recent
        # posts feed (GET /api/hub/posts) directly, not from this per-network lookup.
        base["reason"] = "المنصة لا توفر مقاييس هذا المنشور بالصلاحيات الحالية."
        return base
    if response.status_code >= 400:
        base["reason"] = _error_text(response)
        return base
    body = response.json()
    values = {
        "likes_count": (body.get("likesSummary") or {}).get("totalLikes"),
        "comments_count": (body.get("commentsSummary") or {}).get("totalFirstLevelComments"),
    }
    base.update(values)
    base.update({"available": True, "reason": None, "raw": body})
    return base


async def _publication_metrics(c: SocialCredentials) -> list[dict]:
    jobs: list[tuple[dict, dict]] = []
    for post in list_posts():
        for result in post.get("results") or []:
            jobs.append((post, result))
    if not jobs:
        return []
    timeout = httpx.Timeout(30.0, read=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        values = await asyncio.gather(
            *(_post_metric(client, c, post, result) for post, result in jobs),
            return_exceptions=True,
        )
    output: list[dict] = []
    for (post, result), value in zip(jobs, values):
        if isinstance(value, Exception):
            output.append({
                "hub_post_id": post.get("id"),
                "platform": result.get("platform"),
                "external_post_id": result.get("external_id"),
                "available": False,
                "reason": str(value),
                "likes_count": None,
                "comments_count": None,
                "shares_count": None,
                "views_count": None,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "raw": None,
            })
        else:
            output.append(value)
    return output


async def _save(accounts: list[dict], publications: list[dict]) -> None:
    async with get_sessionmaker()() as session:
        for item in accounts:
            await session.execute(text("""
                INSERT INTO social_metric_snapshots
                (id, platform, captured_at, available, reason, username, followers_count,
                 following_count, posts_count, likes_count, views_count, raw)
                VALUES (:id, :platform, :captured_at, :available, :reason, :username,
                        :followers_count, :following_count, :posts_count, :likes_count,
                        :views_count, CAST(:raw AS JSONB))
            """), {
                **item,
                "id": uuid4(),
                "captured_at": datetime.fromisoformat(item["captured_at"]),
                "raw": __import__("json").dumps(item.get("raw")) if item.get("raw") is not None else None,
            })
        for item in publications:
            await session.execute(text("""
                INSERT INTO publication_metrics
                (id, hub_post_id, platform, external_post_id, captured_at, available,
                 reason, likes_count, comments_count, shares_count, views_count, raw)
                VALUES (:id, :hub_post_id, :platform, :external_post_id, :captured_at,
                        :available, :reason, :likes_count, :comments_count, :shares_count,
                        :views_count, CAST(:raw AS JSONB))
            """), {
                **item,
                "id": uuid4(),
                "captured_at": datetime.fromisoformat(item["captured_at"]),
                "raw": __import__("json").dumps(item.get("raw")) if item.get("raw") is not None else None,
            })
        await session.commit()


async def sync_social_analytics() -> dict:
    credentials = get_social_credentials()
    accounts, publications = await asyncio.gather(
        _account_metrics(credentials),
        _publication_metrics(credentials),
    )
    await _save(accounts, publications)
    history = await analytics_history()
    return {
        "accounts": accounts,
        "publications": publications,
        "history": history,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


async def analytics_history(limit: int = 90) -> list[dict]:
    async with get_sessionmaker()() as session:
        result = await session.execute(text("""
            SELECT platform, captured_at, followers_count, posts_count, likes_count, views_count
            FROM social_metric_snapshots
            WHERE available = TRUE
            ORDER BY captured_at DESC
            LIMIT :limit
        """), {"limit": limit})
        rows = result.all()
    return [
        {
            "platform": row.platform,
            "captured_at": row.captured_at.isoformat(),
            "followers_count": row.followers_count,
            "posts_count": row.posts_count,
            "likes_count": row.likes_count,
            "views_count": row.views_count,
        }
        for row in rows
    ]
