"""Zoho Social integration: OAuth token refresh, the live list of channels already
connected to the Zoho Social portal, and unified publishing for the hub platforms that
are routed through Zoho instead of talking to each network directly.

Docs: https://www.zoho.com/social/api/introduction.html
OAuth: https://www.zoho.com/developer/oauth/self-client/authorization-code-flow.html

This module is a leaf: it only depends on `SocialCredentials` and `httpx`, never on
`app.hub.publish`, so `publish.py` can import it without a circular import.
"""
from __future__ import annotations

import asyncio
import mimetypes
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.hub.credentials import SocialCredentials

# Hub platform id -> Zoho Social network identifier. Covers every network this portal has
# connected. LinkedIn is excluded (Zoho needs linkedinpage vs linkedinprofile, which isn't
# connected here) and keeps its direct integration in publish.py.
NETWORK_MAP: dict[str, str] = {
    "tiktok": "tiktok",
    "youtube": "youtube",
    "twitter": "twitter",
    "facebook": "facebookpage",
    "instagram": "instagram",
    "pinterest": "pinterest",
    "threads": "threadsbyig",
}
PLATFORM_BY_NETWORK = {network: platform for platform, network in NETWORK_MAP.items()}

# access_token (1h-lived) cached per refresh_token so we don't mint a new one on every
# publish/status call. Zoho allows at most 10 refreshes per 10 minutes per refresh token.
_token_cache: dict[str, tuple[str, float]] = {}
# Resolved Pinterest board id cached per portal+brand (Zoho requires one board per post).
_board_cache: dict[str, str] = {}
# Zoho's Channels API does not return a profile link; we derive one per network from that
# network's own most recent published post (its "from.link") and cache it for a while.
_profile_link_cache: dict[str, tuple[str | None, float]] = {}


def managed_platforms(creds: SocialCredentials) -> set[str]:
    """Hub platforms that should be published through Zoho Social right now."""
    if not creds.zoho_ready():
        return set()
    wanted = {n.strip().lower() for n in creds.zoho_networks.split(",") if n.strip()}
    return {platform for platform, network in NETWORK_MAP.items() if network in wanted}


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or f"HTTP {response.status_code}")[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:300]
        if error:
            return str(error)[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return (response.text or f"HTTP {response.status_code}")[:300]


async def _access_token(client: httpx.AsyncClient, creds: SocialCredentials) -> str:
    cache_key = creds.zoho_refresh_token.strip()
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    response = await client.post(
        f"{creds.zoho_accounts_url.rstrip('/')}/oauth/v2/token",
        params={
            "client_id": creds.zoho_client_id.strip(),
            "client_secret": creds.zoho_client_secret.strip(),
            "refresh_token": cache_key,
            "grant_type": "refresh_token",
        },
    )
    data = response.json() if response.content else {}
    token = str(data.get("access_token") or "")
    if response.status_code >= 400 or not token:
        raise RuntimeError(str(data.get("error") or _error_text(response)))
    # Access tokens last 3600s; refresh 10 minutes early to stay safe.
    _token_cache[cache_key] = (token, time.monotonic() + 50 * 60)
    return token


def _headers(token: str, creds: SocialCredentials, *, json_body: bool = True) -> dict:
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "portal_id": creds.zoho_portal_id.strip(),
        "brand_id": creds.zoho_brand_id.strip(),
    }
    if json_body:
        headers["content-type"] = "application/json"
    return headers


async def _profile_link(client: httpx.AsyncClient, creds: SocialCredentials, network: str) -> str | None:
    """Zoho's Channels API has no profile_url field. The most recent published post on each
    network carries a real "from.link" to that channel's actual profile/page — use that."""
    cached = _profile_link_cache.get(network)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    link: str | None = None
    try:
        token = await _access_token(client, creds)
        response = await client.get(
            f"{creds.zoho_api_domain.rstrip('/')}/social/v2/posts/published",
            params={"network": network, "limit": 1, "fields": "from"},
            headers=_headers(token, creds, json_body=False),
        )
        if response.status_code < 400:
            entries = (response.json() or {}).get("data") or []
            if entries:
                link = ((entries[0].get("from") or {}).get("link")) or None
    except Exception:
        link = None
    _profile_link_cache[network] = (link, time.monotonic() + 30 * 60)
    return link


async def get_channels(client: httpx.AsyncClient, creds: SocialCredentials) -> list[dict]:
    """The social accounts already connected to this Zoho Social brand (for display only)."""
    if not creds.zoho_ready():
        return []
    token = await _access_token(client, creds)
    response = await client.get(
        f"{creds.zoho_api_domain.rstrip('/')}/social/v2/channels",
        params={"fields": "id,name,network,status,image_url,profile_id"},
        headers=_headers(token, creds, json_body=False),
    )
    if response.status_code >= 400:
        raise RuntimeError(_error_text(response))
    channels = (response.json() or {}).get("data", [])
    networks = sorted({c.get("network") for c in channels if c.get("network")})
    if networks:
        links = await asyncio.gather(*(_profile_link(client, creds, n) for n in networks), return_exceptions=True)
        link_by_network = {n: (l if not isinstance(l, Exception) else None) for n, l in zip(networks, links)}
        for channel in channels:
            channel["profile_url"] = link_by_network.get(channel.get("network"))
    return channels


async def channel_check(client: httpx.AsyncClient, creds: SocialCredentials) -> dict:
    """Used by /api/hub/status: confirms the token works and lists connected channels."""
    dashboard_url = creds.zoho_api_domain.rstrip("/") or "https://social.zoho.com"
    if not creds.zoho_ready():
        return {
            "ok": False,
            "message": "أضف ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, "
                       "ZOHO_PORTAL_ID, ZOHO_BRAND_ID في .env",
            "channels": [],
            "dashboard_url": dashboard_url,
        }
    try:
        channels = await get_channels(client, creds)
    except Exception as exc:
        return {"ok": False, "message": str(exc) or "تعذر الاتصال بزوهو سوشال", "channels": [], "dashboard_url": dashboard_url}
    return {
        "ok": True,
        "message": f"{len(channels)} حساب متصل عبر Zoho Social",
        "channels": channels,
        "dashboard_url": dashboard_url,
    }


async def _pinterest_board_id(client: httpx.AsyncClient, creds: SocialCredentials) -> str:
    """Zoho requires a board_id on every Pinterest post. Use the override if set, otherwise
    reuse the brand's first board (https://www.zoho.com/social/api/pinterest-boards.html)."""
    override = creds.zoho_pinterest_board_id.strip()
    if override:
        return override
    cache_key = f"{creds.zoho_portal_id.strip()}:{creds.zoho_brand_id.strip()}"
    cached = _board_cache.get(cache_key)
    if cached:
        return cached
    token = await _access_token(client, creds)
    response = await client.get(
        f"{creds.zoho_api_domain.rstrip('/')}/social/v2/pinterest/boards",
        params={"limit": 1, "fields": "id,name"},
        headers=_headers(token, creds, json_body=False),
    )
    if response.status_code >= 400:
        raise RuntimeError(_error_text(response))
    boards = (response.json() or {}).get("data") or []
    if not boards:
        raise RuntimeError("Zoho Social: لا توجد لوحة بنترست متاحة. أنشئ لوحة أو اضبط ZOHO_PINTEREST_BOARD_ID.")
    board_id = str(boards[0]["id"])
    _board_cache[cache_key] = board_id
    return board_id


def _media_object(media_url: str, media_kind: str, byte_length: int | None) -> dict:
    name = (urlparse(media_url).path.rsplit("/", 1)[-1] or f"natheel.{('mp4' if media_kind == 'video' else 'jpg')}")
    guessed_mime = mimetypes.guess_type(name)[0] or ("video/mp4" if media_kind == "video" else "image/jpeg")
    fmt = guessed_mime.split("/")[-1]
    return {
        "file_path": media_url,
        "name": name,
        "size": byte_length or 0,
        "type": media_kind,
        "format": fmt,
        "width": 0,
        "height": 0,
    }


async def publish(
    creds: SocialCredentials,
    client: httpx.AsyncClient,
    platforms: list[str],
    *,
    caption: str,
    media_url: str | None,
    media_kind: str | None,
    media_bytes_len: int | None,
) -> dict[str, dict]:
    """Publish one post to the requested hub platforms via the Zoho Social unified API.

    Returns {hub_platform_id: {"ok": bool, "message": str, "post_id": str | None}}.
    """
    networks = sorted({NETWORK_MAP[p] for p in platforms if p in NETWORK_MAP})
    if not networks:
        return {}
    token = await _access_token(client, creds)

    # A Pinterest channel needs a board_id; resolve it without letting a Pinterest-only
    # problem block the other networks in the same post.
    pre_failed: dict[str, dict] = {}
    channels: list[dict] = []
    for network in networks:
        channel = {"network": network, "brand_id": creds.zoho_brand_id.strip()}
        if network == "pinterest":
            try:
                channel["board_id"] = await _pinterest_board_id(client, creds)
            except Exception as exc:
                pre_failed["pinterest"] = {"ok": False, "message": str(exc) or "تعذر تحديد لوحة بنترست", "post_id": None}
                continue
        channels.append(channel)
    if not channels:
        return pre_failed

    message_obj: dict = {"channels": channels, "message": caption, "has_media": media_kind or "none"}
    if media_url:
        message_obj["medias"] = [_media_object(media_url, media_kind or "image", media_bytes_len)]
    response = await client.post(
        f"{creds.zoho_api_domain.rstrip('/')}/social/v2/posts/publish",
        json={"post": {"messages": [message_obj]}},
        headers=_headers(token, creds),
    )
    sent_networks = [c["network"] for c in channels]
    if response.status_code >= 400:
        failure = _error_text(response)
        return {
            **pre_failed,
            **{PLATFORM_BY_NETWORK[n]: {"ok": False, "message": failure, "post_id": None} for n in sent_networks},
        }

    body = response.json() if response.content else {}
    entries = body.get("data") or []
    entry = entries[0] if entries else {}
    overall_ok = bool(entry.get("success"))
    overall_message = str(entry.get("message") or ("تم النشر عبر Zoho Social" if overall_ok else "تعذر النشر عبر Zoho Social"))
    post_id = entry.get("post_id")

    results = {
        PLATFORM_BY_NETWORK[n]: {"ok": overall_ok, "message": overall_message, "post_id": post_id}
        for n in sent_networks
    }
    if overall_ok and post_id:
        await _refine_with_publish_status(client, creds, token, str(post_id), results)
    results.update(pre_failed)
    return results


def _normalize_published_post(network: str, entry: dict) -> dict:
    from_obj = entry.get("from") or {}
    media_url: str | None = None
    media_kind: str | None = None
    for attachment in (entry.get("attachments") or {}).get("data") or []:
        for item in (attachment.get("media") or {}).get("data") or []:
            image = item.get("image") or {}
            video = item.get("video") or {}
            thumbnail = video.get("thumbnail") or {}
            if image.get("src"):
                media_url, media_kind = image["src"], "image"
                break
            if thumbnail.get("src"):
                media_url, media_kind = thumbnail["src"], "video"
                break
        if media_url:
            break
    created_at = None
    created_ms = entry.get("created_time")
    if isinstance(created_ms, (int, float)):
        created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
    post_id = str(entry.get("id") or "")
    return {
        "id": f"zoho:{network}:{post_id}",
        "source": "zoho",
        "external_id": post_id,
        "platform": PLATFORM_BY_NETWORK.get(network, network),
        "network": network,
        "caption": entry.get("message") or entry.get("description") or "",
        "media_url": media_url,
        "media_type": media_kind or "text",
        "permalink": entry.get("perma_link"),
        "manage_url": entry.get("perma_link") or from_obj.get("link"),
        "created_at": created_at,
        "from_name": from_obj.get("name"),
        "stats": {
            "likes_count": entry.get("like_count"),
            "comments_count": entry.get("comment_count"),
            "shares_count": entry.get("share_count") if entry.get("share_count") is not None else entry.get("retweet_count"),
            "views_count": entry.get("view_count"),
        },
    }


async def recent_posts(
    client: httpx.AsyncClient, creds: SocialCredentials, *, limit_per_network: int = 10, total_limit: int = 30,
) -> list[dict]:
    """Live published posts pulled directly from Zoho Social (not our local record), merged
    across every network Zoho manages for this brand and sorted newest first."""
    if not creds.zoho_ready():
        return []
    networks = sorted({NETWORK_MAP[p] for p in managed_platforms(creds)})
    if not networks:
        return []
    token = await _access_token(client, creds)

    async def _one(network: str) -> list[dict]:
        try:
            response = await client.get(
                f"{creds.zoho_api_domain.rstrip('/')}/social/v2/posts/published",
                params={
                    "network": network,
                    "limit": limit_per_network,
                    "fields": "id,message,description,attachments,perma_link,created_time,"
                              "like_count,comment_count,share_count,retweet_count,view_count,from",
                },
                headers=_headers(token, creds, json_body=False),
            )
            if response.status_code >= 400:
                return []
            return [_normalize_published_post(network, entry) for entry in (response.json() or {}).get("data") or []]
        except Exception:
            return []

    groups = await asyncio.gather(*(_one(network) for network in networks))
    posts = [post for group in groups for post in group]
    posts.sort(key=lambda post: post.get("created_at") or "", reverse=True)
    return posts[:total_limit]


async def _refine_with_publish_status(
    client: httpx.AsyncClient, creds: SocialCredentials, token: str, post_id: str, results: dict[str, dict],
) -> None:
    """Best effort: Zoho reports one success flag per brand, not per network. Poll Get
    Publish Status once to split that into a real per-network outcome when possible."""
    try:
        response = await client.get(
            f"{creds.zoho_api_domain.rstrip('/')}/social/v2/posts/publish/{post_id}",
            headers=_headers(token, creds, json_body=False),
        )
        if response.status_code >= 400:
            return
        for entry in (response.json() or {}).get("data", []):
            platform = PLATFORM_BY_NETWORK.get(str(entry.get("network") or ""))
            if platform is None or platform not in results:
                continue
            status = str(entry.get("status") or "")
            error = entry.get("error") or {}
            results[platform] = {
                "ok": status in {"published", "scheduled"} and not error,
                "message": str(error.get("message")) if error else f"Zoho Social: {status or 'تم النشر'}",
                "post_id": entry.get("post_id") or results[platform].get("post_id"),
            }
    except Exception:  # the publish already succeeded; a status-check hiccup is not fatal
        return
