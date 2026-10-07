"""Live, nullable social analytics. No synthetic values are produced."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.hub.credentials import SocialCredentials, get_social_credentials
from app.hub.publish import (
    _error_text,
    _oauth1_header,
    _snapchat_bearer,
    _youtube_token,
)
from app.hub.store import list_posts

PLATFORM_NAMES = {
    "tiktok": "تيك توك",
    "snapchat": "سناب شات",
    "instagram": "إنستغرام",
    "linkedin": "لينكد إن",
    "twitter": "إكس",
    "pinterest": "بنترست",
    "youtube": "يوتيوب",
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


async def _instagram(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("meta_page_access_token", "instagram_business_account_id"):
        return _empty("instagram", "بيانات اعتماد إنستغرام غير مكتملة.")
    url = f"https://graph.facebook.com/{c.meta_graph_version.strip()}/{c.instagram_business_account_id.strip()}"
    response = await client.get(
        url,
        params={
            "fields": "username,followers_count,follows_count,media_count",
            "access_token": c.meta_page_access_token.strip(),
        },
    )
    if response.status_code >= 400:
        return _empty("instagram", _error_text(response), connected=True)
    body = response.json()
    return _ready("instagram", {
        "username": body.get("username"),
        "followers_count": body.get("followers_count"),
        "following_count": body.get("follows_count"),
        "posts_count": body.get("media_count"),
    }, body)


async def _tiktok(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("tiktok_access_token"):
        return _empty("tiktok", "TIKTOK_ACCESS_TOKEN غير موجود.")
    response = await client.get(
        "https://open.tiktokapis.com/v2/user/info/",
        params={"fields": "display_name,username,follower_count,following_count,likes_count,video_count"},
        headers={"Authorization": f"Bearer {c.tiktok_access_token.strip()}"},
    )
    if response.status_code >= 400:
        return _empty("tiktok", _error_text(response), connected=True)
    body = response.json()
    error = body.get("error") or {}
    if error.get("code") not in {None, "", "ok"}:
        return _empty("tiktok", str(error.get("message") or error), connected=True)
    user = ((body.get("data") or {}).get("user") or {})
    return _ready("tiktok", {
        "username": user.get("username") or user.get("display_name"),
        "followers_count": user.get("follower_count"),
        "following_count": user.get("following_count"),
        "posts_count": user.get("video_count"),
        "likes_count": user.get("likes_count"),
    }, body)


async def _youtube(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not (
        c.filled("youtube_access_token")
        or c.filled("youtube_refresh_token", "google_client_id", "google_client_secret")
    ):
        return _empty("youtube", "بيانات اعتماد يوتيوب غير مكتملة.")
    try:
        token = await _youtube_token(client, c)
    except Exception as exc:
        return _empty("youtube", str(exc), connected=True)
    response = await client.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet,statistics", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code >= 400:
        return _empty("youtube", _error_text(response), connected=True)
    body = response.json()
    item = next(iter(body.get("items") or []), {})
    stats = item.get("statistics") or {}
    return _ready("youtube", {
        "username": (item.get("snippet") or {}).get("title"),
        "followers_count": int(stats["subscriberCount"]) if stats.get("subscriberCount") is not None else None,
        "posts_count": int(stats["videoCount"]) if stats.get("videoCount") is not None else None,
        "views_count": int(stats["viewCount"]) if stats.get("viewCount") is not None else None,
    }, body)


async def _pinterest(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("pinterest_access_token"):
        return _empty("pinterest", "PINTEREST_ACCESS_TOKEN غير موجود.")
    response = await client.get(
        "https://api.pinterest.com/v5/user_account",
        headers={"Authorization": f"Bearer {c.pinterest_access_token.strip()}"},
    )
    if response.status_code >= 400:
        return _empty("pinterest", _error_text(response), connected=True)
    body = response.json()
    return _ready("pinterest", {
        "username": body.get("username"),
        "followers_count": body.get("follower_count"),
        "following_count": body.get("following_count"),
        "posts_count": body.get("pin_count"),
    }, body)


async def _twitter(client: httpx.AsyncClient, c: SocialCredentials) -> dict:
    if not c.filled("x_api_key", "x_api_secret", "x_access_token", "x_access_token_secret"):
        return _empty("twitter", "مفاتيح OAuth 1.0a لمنصة إكس غير مكتملة.")
    url = "https://api.twitter.com/2/users/me"
    query = {"user.fields": "public_metrics,username,name"}
    response = await client.get(
        url,
        params=query,
        headers={"Authorization": _oauth1_header("GET", url, c, query)},
    )
    if response.status_code >= 400:
        return _empty("twitter", _error_text(response), connected=True)
    body = response.json()
    user = body.get("data") or {}
    metrics = user.get("public_metrics") or {}
    return _ready("twitter", {
        "username": user.get("username"),
        "followers_count": metrics.get("followers_count"),
        "following_count": metrics.get("following_count"),
        "posts_count": metrics.get("tweet_count"),
        "likes_count": metrics.get("like_count"),
    }, body)


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
        calls = (
            _tiktok(client, c),
            _snapchat(client, c),
            _instagram(client, c),
            _linkedin(client, c),
            _twitter(client, c),
            _pinterest(client, c),
            _youtube(client, c),
        )
        results = await asyncio.gather(*calls, return_exceptions=True)
    metrics: list[dict] = []
    for platform, result in zip(PLATFORM_NAMES, results):
        if isinstance(result, Exception):
            metrics.append(_empty(platform, str(result)))
        else:
            metrics.append(result)
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
    if platform == "instagram" and c.filled("meta_page_access_token"):
        response = await client.get(
            f"https://graph.facebook.com/{c.meta_graph_version.strip()}/{external_id}",
            params={"fields": "like_count,comments_count", "access_token": c.meta_page_access_token.strip()},
        )
    elif platform == "twitter" and c.filled("x_api_key", "x_api_secret", "x_access_token", "x_access_token_secret"):
        url = f"https://api.twitter.com/2/tweets/{external_id}"
        query = {"tweet.fields": "public_metrics"}
        response = await client.get(url, params=query, headers={"Authorization": _oauth1_header("GET", url, c, query)})
    elif platform == "youtube":
        try:
            token = await _youtube_token(client, c)
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics", "id": external_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            base["reason"] = str(exc)
            return base
    elif platform == "linkedin" and c.filled("linkedin_access_token"):
        response = await client.get(
            f"https://api.linkedin.com/rest/socialActions/{quote(external_id, safe='')}",
            headers={
                "Authorization": f"Bearer {c.linkedin_access_token.strip()}",
                "LinkedIn-Version": c.linkedin_api_version.strip() or "202506",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )
    else:
        base["reason"] = "المنصة لا توفر مقاييس هذا المنشور بالصلاحيات الحالية."
        return base
    if response.status_code >= 400:
        base["reason"] = _error_text(response)
        return base
    body = response.json()
    values: dict = {}
    if platform == "instagram":
        values = {"likes_count": body.get("like_count"), "comments_count": body.get("comments_count")}
    elif platform == "twitter":
        metric = ((body.get("data") or {}).get("public_metrics") or {})
        values = {
            "likes_count": metric.get("like_count"),
            "comments_count": metric.get("reply_count"),
            "shares_count": metric.get("retweet_count"),
            "views_count": metric.get("impression_count"),
        }
    elif platform == "youtube":
        metric = (next(iter(body.get("items") or []), {}).get("statistics") or {})
        values = {
            "likes_count": int(metric["likeCount"]) if metric.get("likeCount") is not None else None,
            "comments_count": int(metric["commentCount"]) if metric.get("commentCount") is not None else None,
            "views_count": int(metric["viewCount"]) if metric.get("viewCount") is not None else None,
        }
    elif platform == "linkedin":
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
