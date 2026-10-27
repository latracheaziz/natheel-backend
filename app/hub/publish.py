"""Publish one hub post to the social networks selected in the admin form.

Facebook, Instagram, X, YouTube, Pinterest, TikTok, and Threads are published exclusively
through the connected Zoho Social portal (see `zoho_social.py`) — there is no direct-API
fallback for them here anymore. Only LinkedIn and Snapchat, which are not connected inside
Zoho, still call their own network APIs directly below.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

from app.hub import zoho_social
from app.hub.credentials import SocialCredentials
from app.integrations.social.linkedin import escape_commentary

PLATFORMS = (
    "tiktok", "snapchat", "instagram", "linkedin", "twitter", "pinterest", "youtube",
    "facebook", "threads",
)


@dataclass
class PlatformResult:
    platform: str
    status: str
    message: str
    external_id: str | None = None
    url: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class HubContent:
    caption: str
    image_bytes: bytes | None = None
    image_mime: str | None = None
    video_bytes: bytes | None = None
    video_mime: str | None = None
    video_url: str | None = None

    @property
    def title(self) -> str:
        line = next((part.strip() for part in self.caption.splitlines() if part.strip()), "")
        return (line or "منشور نثيل")[:100]


def _missing(platform: str, keys: str) -> PlatformResult:
    return PlatformResult(
        platform, "missing_credentials",
        f"أضف القيم في الملف .env ثم أعد النشر: {keys}",
    )


def _unsupported(platform: str, message: str) -> PlatformResult:
    return PlatformResult(platform, "unsupported", message)


def _failed(platform: str, message: str) -> PlatformResult:
    return PlatformResult(platform, "failed", message[:400])


def _published(platform: str, external_id: str, url: str | None = None, message: str = "تم النشر") -> PlatformResult:
    return PlatformResult(platform, "published", message, external_id, url)


def is_public_http_url(url: str | None) -> bool:
    if not url or not url.startswith("https://"):
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local"):
        return False
    return True


def _error_text(response: httpx.Response) -> str:
    grpc_message = (response.headers.get("grpc-message") or "").strip()
    try:
        data = response.json()
    except ValueError:
        return (grpc_message or response.text or f"HTTP {response.status_code}")[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("error_user_msg") or error.get("code") or error)[:300]
        if error:
            return str(error)[:300]
        for key in ("display_message", "debug_message", "error_code", "detail", "message", "title"):
            if data.get(key):
                return str(data[key])[:300]
    return (response.text or f"HTTP {response.status_code}")[:300]


async def _publish_linkedin(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    if not creds.filled("linkedin_access_token", "linkedin_author_urn"):
        return _missing("linkedin", "LINKEDIN_ACCESS_TOKEN, LINKEDIN_AUTHOR_URN")
    author = creds.linkedin_author()
    headers = {
        "Authorization": f"Bearer {creds.linkedin_access_token.strip()}",
        "LinkedIn-Version": creds.linkedin_api_version.strip() or "202506",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    media = None
    if content.video_bytes:
        media = {"media": {"id": await _linkedin_video(client, headers, author, content.video_bytes)}}
    elif content.image_bytes:
        media = {"media": {"id": await _linkedin_image(client, headers, author, content.image_bytes)}}
    body = {
        "author": author,
        "commentary": escape_commentary(content.caption.strip() or " "),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if media:
        body["content"] = media
    response = await client.post("https://api.linkedin.com/rest/posts", json=body, headers=headers)
    if response.status_code >= 400:
        return _failed("linkedin", _error_text(response))
    post_urn = response.headers.get("x-restli-id") or ""
    url = f"https://www.linkedin.com/feed/update/{post_urn}" if post_urn else None
    return _published("linkedin", post_urn, url)


async def _linkedin_image(client: httpx.AsyncClient, headers: dict, author: str, data: bytes) -> str:
    init = await client.post(
        "https://api.linkedin.com/rest/images?action=initializeUpload",
        json={"initializeUploadRequest": {"owner": author}},
        headers=headers,
    )
    if init.status_code >= 400:
        raise RuntimeError(_error_text(init))
    value = init.json()["value"]
    uploaded = await client.put(value["uploadUrl"], content=data, headers={"Authorization": headers["Authorization"]})
    if uploaded.status_code >= 400:
        raise RuntimeError(_error_text(uploaded))
    return value["image"]


async def _linkedin_video(client: httpx.AsyncClient, headers: dict, author: str, data: bytes) -> str:
    init = await client.post(
        "https://api.linkedin.com/rest/videos?action=initializeUpload",
        json={"initializeUploadRequest": {
            "owner": author, "fileSizeBytes": len(data), "uploadCaptions": False, "uploadThumbnail": False}},
        headers=headers,
    )
    if init.status_code >= 400:
        raise RuntimeError(_error_text(init))
    value = init.json()["value"]
    etags = []
    for part in value.get("uploadInstructions") or []:
        first, last = int(part["firstByte"]), int(part["lastByte"])
        uploaded = await client.put(
            part["uploadUrl"], content=data[first:last + 1],
            headers={"Authorization": headers["Authorization"], "Content-Type": "application/octet-stream"},
        )
        if uploaded.status_code >= 400:
            raise RuntimeError(_error_text(uploaded))
        etags.append((uploaded.headers.get("etag") or "").strip('"'))
    finalized = await client.post(
        "https://api.linkedin.com/rest/videos?action=finalizeUpload",
        json={"finalizeUploadRequest": {
            "video": value["video"], "uploadToken": value.get("uploadToken", ""), "uploadedPartIds": etags}},
        headers=headers,
    )
    if finalized.status_code >= 400:
        raise RuntimeError(_error_text(finalized))
    return value["video"]


SNAPCHAT_API = "https://businessapi.snapchat.com"
_SNAPCHAT_CHUNK = 32 * 1024 * 1024


def _snapchat_token_message() -> str:
    return "أضف SNAPCHAT_CLIENT_ID و SNAPCHAT_ACCESS_TOKEN و SNAPCHAT_PROFILE_ID في .env."


# Fresh access tokens live only in this process. .env keeps the long-lived OAuth values.
_snapchat_access_cache: dict[str, tuple[str, float]] = {}


def _encrypt_snapchat_media(data: bytes) -> tuple[bytes, str, str]:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = secrets.token_bytes(32)
    iv = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return encrypted, base64.b64encode(key).decode(), base64.b64encode(iv).decode()


def _snapchat_oauth_material(creds: SocialCredentials) -> tuple[str, str, str]:
    """Client id, client secret, and the long-lived refresh token from .env."""
    client_id = creds.snapchat_client_id.strip()
    client_secret = creds.snapchat_client_secret.strip()
    # Business Manager may place the long-lived OAuth token in either field.
    refresh_token = creds.snapchat_refresh_token.strip() or creds.snapchat_access_token.strip()
    return client_id, client_secret, refresh_token


async def _snapchat_bearer(
    creds: SocialCredentials,
    client: httpx.AsyncClient,
    *,
    force: bool = False,
) -> tuple[str | None, str | None]:
    """Create a short-lived access token from the OAuth app credentials. Returns (token, error)."""
    client_id, client_secret, refresh_token = _snapchat_oauth_material(creds)
    saved_access = creds.snapchat_access_token.strip()
    if not refresh_token and not saved_access:
        return None, _snapchat_token_message()
    if not client_id:
        return (saved_access or None), (None if saved_access else _snapchat_token_message())
    cache_key = hashlib.sha256(f"{client_id}:{refresh_token}:{client_secret}".encode()).hexdigest()
    cached = _snapchat_access_cache.get(cache_key)
    if cached and cached[1] > time.time() and not force:
        return cached[0], None
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    response = await client.post("https://accounts.snapchat.com/login/oauth2/access_token", data=form)
    if response.status_code < 400:
        payload = response.json() if response.content else {}
        token = str(payload.get("access_token") or "").strip()
        if token:
            expires_in = int(payload.get("expires_in") or 3600)
            _snapchat_access_cache[cache_key] = (token, time.time() + max(expires_in - 120, 60))
            return token, None
    if saved_access:
        return saved_access, None
    return None, _error_text(response) or _snapchat_token_message()


def _snapchat_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _snapchat_failed(response: httpx.Response, creds: SocialCredentials) -> PlatformResult:
    detail = _error_text(response)
    if response.status_code in {401, 403} and not creds.snapchat_client_secret.strip():
        detail = (
            f"{detail}. الرمز المحفوظ مرفوض من سناب شات. "
            "أضف SNAPCHAT_CLIENT_SECRET من نفس تطبيق OAuth ليتم إنشاء رمز جديد في كل طلب."
        )
    return _failed("snapchat", detail)


async def snapchat_account_check(creds: SocialCredentials) -> dict:
    """Confirm a freshly generated Snapchat token can read the public profile. Does not post."""
    if not creds.filled("snapchat_profile_id"):
        return {"ok": False, "message": "أضف SNAPCHAT_PROFILE_ID في .env"}
    profile = creds.snapchat_profile_id.strip()
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        token, error = await _snapchat_bearer(creds, client, force=True)
        if not token:
            return {"ok": False, "message": error or _snapchat_token_message()}
        response = await client.get(
            f"{SNAPCHAT_API}/v1/public_profiles/{profile}",
            headers=_snapchat_headers(token),
        )
        if response.status_code in {401, 403}:
            return {"ok": False, "message": _snapchat_failed(response, creds).message}
        if response.status_code >= 400:
            return {"ok": False, "message": _error_text(response)}
        return {"ok": True, "message": "حساب سناب شات متصل"}


async def _snapchat_request(
    client: httpx.AsyncClient,
    creds: SocialCredentials,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    token, error = await _snapchat_bearer(creds, client)
    if not token:
        raise RuntimeError(error or _snapchat_token_message())
    response = await client.request(method, url, headers=_snapchat_headers(token), **kwargs)
    if response.status_code not in {401, 403}:
        return response
    token, error = await _snapchat_bearer(creds, client, force=True)
    if not token:
        return response
    return await client.request(method, url, headers=_snapchat_headers(token), **kwargs)


async def _publish_snapchat(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    if not creds.filled("snapchat_profile_id"):
        return _missing("snapchat", "SNAPCHAT_PROFILE_ID")
    if not (creds.filled("snapchat_access_token") or creds.filled("snapchat_refresh_token", "snapchat_client_id", "snapchat_client_secret")):
        return _missing("snapchat", "SNAPCHAT_CLIENT_ID, SNAPCHAT_ACCESS_TOKEN, SNAPCHAT_PROFILE_ID")
    media = content.video_bytes or content.image_bytes
    if not media and content.video_url:
        return _failed("snapchat", "تعذر قراءة ملف الفيديو لإرساله إلى سناب شات.")
    if not media:
        return _unsupported("snapchat", "سناب شات يتطلب صورة أو فيديو.")
    profile = creds.snapchat_profile_id.strip()
    kind = "VIDEO" if content.video_bytes else "IMAGE"
    encrypted, key, iv = _encrypt_snapchat_media(media)
    created = await _snapchat_request(
        client, creds, "POST",
        f"{SNAPCHAT_API}/v1/public_profiles/{profile}/media",
        json={"type": kind, "name": content.title[:80] or "natheel", "key": key, "iv": iv},
    )
    if created.status_code >= 400:
        return _snapchat_failed(created, creds)
    created_body = created.json() if created.content else {}
    if str(created_body.get("request_status") or "").upper() == "ERROR":
        return _failed("snapchat", str(created_body.get("display_message") or created_body.get("debug_message") or "تعذر تجهيز الوسائط"))
    media_id = str(created_body.get("media_id") or "")
    add_path = str(created_body.get("add_path") or "")
    finalize_path = str(created_body.get("finalize_path") or add_path)
    if not media_id or not add_path:
        return _failed("snapchat", "سناب شات لم يُرجع معرّف الوسائط.")

    def _absolute(path: str) -> str:
        return path if path.startswith("http") else f"{SNAPCHAT_API}{path if path.startswith('/') else '/' + path}"

    part = 1
    for start in range(0, len(encrypted), _SNAPCHAT_CHUNK):
        chunk = encrypted[start:start + _SNAPCHAT_CHUNK]
        uploaded = await _snapchat_request(
            client, creds, "POST", _absolute(add_path),
            files={
                "action": (None, "ADD"),
                "part_number": (None, str(part)),
                "file": (f"media.enc.{part}", chunk, "application/octet-stream"),
            },
        )
        if uploaded.status_code >= 400:
            return _snapchat_failed(uploaded, creds)
        part += 1
    finalized = await _snapchat_request(
        client, creds, "POST", _absolute(finalize_path),
        files={"action": (None, "FINALIZE")},
    )
    if finalized.status_code >= 400:
        return _snapchat_failed(finalized, creds)
    posted = await _snapchat_request(
        client, creds, "POST",
        f"{SNAPCHAT_API}/v1/public_profiles/{profile}/stories",
        json={"media_id": media_id},
    )
    if posted.status_code >= 400:
        return _snapchat_failed(posted, creds)
    body = posted.json() if posted.content else {}
    if str(body.get("request_status") or "SUCCESS").upper() == "ERROR":
        return _failed("snapchat", str(body.get("display_message") or body.get("error_code") or "تعذر نشر القصة"))
    return _published("snapchat", str(body.get("request_id") or media_id))


def _zoho_only(platform: str) -> PlatformResult:
    # Facebook, Instagram, X, YouTube, Pinterest, TikTok, and Threads have no direct
    # integration in the hub anymore; they only publish through the connected Zoho Social
    # portal. This fires only if ZOHO_* credentials in .env are missing/incomplete.
    return PlatformResult(
        platform, "missing_credentials",
        "هذه المنصة تُنشر فقط عبر Zoho Social. أضف ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, "
        "ZOHO_REFRESH_TOKEN, ZOHO_PORTAL_ID, ZOHO_BRAND_ID في .env.",
    )


async def _publish_via_zoho(
    platforms: list[str], content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient,
) -> list[PlatformResult]:
    """Fan a single post out to several networks in one Zoho Social API call.

    Images are sent as raw bytes and uploaded straight to Zoho (no public URL or local
    file needed). Zoho Social's API has no direct video-upload endpoint, so a video must
    already be a public HTTPS URL that Zoho can fetch."""
    media_kind: str | None = None
    media_url: str | None = None
    media_bytes: bytes | None = None
    media_mime: str | None = None
    if content.video_url:
        if not is_public_http_url(content.video_url):
            message = (
                "فيديو Zoho Social يجب أن يكون رابط HTTPS عاماً — رفع ملف فيديو مباشرة غير "
                "مدعوم عبر واجهة Zoho Social الحالية (الصق رابط فيديو عاماً بدلاً من رفع ملف)."
            )
            return [_failed(platform, message) for platform in platforms]
        media_kind, media_url = "video", content.video_url
    elif content.image_bytes:
        media_kind, media_bytes, media_mime = "image", content.image_bytes, content.image_mime
    try:
        outcomes = await zoho_social.publish(
            creds, client, platforms,
            caption=content.caption, media_kind=media_kind,
            media_url=media_url, media_bytes=media_bytes, media_mime=media_mime,
        )
    except Exception as exc:
        message = str(exc) or "تعذر الاتصال بزوهو سوشال"
        return [_failed(platform, message) for platform in platforms]
    results: list[PlatformResult] = []
    for platform in platforms:
        outcome = outcomes.get(platform)
        if not outcome:
            results.append(_failed(platform, "زوهو سوشال لم يُرجع نتيجة لهذه المنصة."))
        elif outcome.get("ok"):
            results.append(_published(platform, str(outcome.get("post_id") or ""), message=outcome.get("message") or "تم النشر عبر Zoho Social"))
        else:
            results.append(_failed(platform, outcome.get("message") or "تعذر النشر عبر Zoho Social"))
    return results


# Only the networks NOT connected inside Zoho Social still publish directly.
_PUBLISHERS = {
    "linkedin": _publish_linkedin,
    "snapchat": _publish_snapchat,
}


async def publish_everywhere(platforms: list[str], content: HubContent, creds: SocialCredentials) -> list[PlatformResult]:
    timeout = httpx.Timeout(60.0, read=180.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # Platforms configured to go through the connected Zoho Social portal (see
        # ZOHO_NETWORKS in .env) are fanned out in a single Zoho API call instead of
        # calling TikTok/YouTube/X directly.
        zoho_targets = [p for p in platforms if p in zoho_social.managed_platforms(creds)]
        results: list[PlatformResult] = []
        if zoho_targets:
            results.extend(await _publish_via_zoho(zoho_targets, content, creds, client))
        for platform in platforms:
            if platform in zoho_targets:
                continue
            publisher = _PUBLISHERS.get(platform)
            if publisher is None:
                if platform in zoho_social.NETWORK_MAP:
                    results.append(_zoho_only(platform))
                else:
                    results.append(_failed(platform, "منصة غير معروفة."))
                continue
            try:
                results.append(await publisher(content, creds, client))
            except Exception as exc:  # surface provider failures per network, keep the others going
                results.append(_failed(platform, str(exc) or "تعذر النشر"))
        order = {platform: index for index, platform in enumerate(platforms)}
        results.sort(key=lambda item: order.get(item.platform, len(order)))
        return results
