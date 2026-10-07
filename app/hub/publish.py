"""Publish one hub post to the social networks selected in the admin form."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlparse

import httpx

from app.hub.credentials import SocialCredentials
from app.integrations.social.linkedin import escape_commentary

PLATFORMS = ("tiktok", "snapchat", "instagram", "linkedin", "twitter", "pinterest", "youtube")


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
    image_url: str | None = None
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
        f"أضف القيم في الملف social.env ثم أعد النشر: {keys}",
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


def _public_media_message(platform_name: str) -> str:
    return (
        f"{platform_name} يجب أن يحمّل الصورة أو الفيديو من رابط HTTPS عام. "
        "ضع MEDIA_PUBLIC_BASE_URL في social.env (رابط عام يصل إلى هذا الخادم)، "
        "أو أرفق رابط فيديو/صورة عاماً."
    )


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


def _oauth1_header(method: str, url: str, creds: SocialCredentials, extra: dict | None = None) -> str:
    oauth = {
        "oauth_consumer_key": creds.x_api_key.strip(),
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds.x_access_token.strip(),
        "oauth_version": "1.0",
    }
    collected = {**oauth, **(extra or {})}

    def enc(value: str) -> str:
        return quote(str(value), safe="")

    param_string = "&".join(f"{enc(key)}={enc(collected[key])}" for key in sorted(collected))
    base = "&".join([method.upper(), enc(url.split("?")[0]), enc(param_string)])
    signing_key = f"{enc(creds.x_api_secret.strip())}&{enc(creds.x_access_token_secret.strip())}"
    signature = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{enc(key)}="{enc(oauth[key])}"' for key in sorted(oauth))


async def _youtube_token(client: httpx.AsyncClient, creds: SocialCredentials) -> str:
    if creds.youtube_access_token.strip():
        return creds.youtube_access_token.strip()
    response = await client.post("https://oauth2.googleapis.com/token", data={
        "client_id": creds.google_client_id.strip(),
        "client_secret": creds.google_client_secret.strip(),
        "refresh_token": creds.youtube_refresh_token.strip(),
        "grant_type": "refresh_token",
    })
    if response.status_code >= 400:
        raise RuntimeError(_error_text(response))
    return str(response.json().get("access_token") or "")


async def _publish_twitter(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    keys = "X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET"
    if not creds.filled("x_api_key", "x_api_secret", "x_access_token", "x_access_token_secret"):
        return _missing("twitter", keys)
    media_ids: list[str] = []
    if content.video_bytes:
        media_ids.append(await _twitter_video(client, creds, content.video_bytes, content.video_mime or "video/mp4"))
    elif content.image_bytes:
        media_ids.append(await _twitter_image(client, creds, content.image_bytes))
    text = content.caption.strip()
    if len(text) > 280:
        text = text[:277] + "..."
    body: dict = {}
    if text:
        body["text"] = text
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    if not body:
        return _unsupported("twitter", "أضف نصاً أو صورة أو فيديو.")
    response = await client.post(
        "https://api.twitter.com/2/tweets",
        json=body,
        headers={"Authorization": _oauth1_header("POST", "https://api.twitter.com/2/tweets", creds)},
    )
    if response.status_code >= 400:
        return _failed("twitter", _error_text(response))
    tweet_id = str(response.json().get("data", {}).get("id") or "")
    note = "تم النشر" if len(content.caption.strip()) <= 280 else "تم النشر بعد اختصار النص إلى 280 حرفاً لقيود منصة إكس."
    return _published("twitter", tweet_id, f"https://x.com/i/status/{tweet_id}" if tweet_id else None, note)


async def _twitter_image(client: httpx.AsyncClient, creds: SocialCredentials, data: bytes) -> str:
    url = "https://upload.twitter.com/1.1/media/upload.json"
    response = await client.post(
        url,
        files={"media": ("image", data)},
        headers={"Authorization": _oauth1_header("POST", url, creds)},
    )
    if response.status_code >= 400:
        raise RuntimeError(_error_text(response))
    return str(response.json()["media_id_string"])


async def _twitter_video(client: httpx.AsyncClient, creds: SocialCredentials, data: bytes, mime: str) -> str:
    url = "https://upload.twitter.com/1.1/media/upload.json"
    init = {"command": "INIT", "total_bytes": str(len(data)), "media_type": mime, "media_category": "tweet_video"}
    opened = await client.post(url, data=init, headers={"Authorization": _oauth1_header("POST", url, creds, init)})
    if opened.status_code >= 400:
        raise RuntimeError(_error_text(opened))
    media_id = str(opened.json()["media_id_string"])
    chunk = 4 * 1024 * 1024
    for index, start in enumerate(range(0, len(data), chunk)):
        append = {"command": "APPEND", "media_id": media_id, "segment_index": str(index)}
        part = await client.post(
            url,
            data=append,
            files={"media": ("video", data[start:start + chunk], mime)},
            headers={"Authorization": _oauth1_header("POST", url, creds)},
        )
        if part.status_code >= 400:
            raise RuntimeError(_error_text(part))
    finalize = {"command": "FINALIZE", "media_id": media_id}
    closed = await client.post(url, data=finalize, headers={"Authorization": _oauth1_header("POST", url, creds, finalize)})
    if closed.status_code >= 400:
        raise RuntimeError(_error_text(closed))
    processing = closed.json().get("processing_info")
    for _ in range(20):
        if not processing or processing.get("state") in {None, "succeeded"}:
            return media_id
        if processing.get("state") == "failed":
            raise RuntimeError(str(processing.get("error") or "Twitter failed to process the video."))
        await _sleep(float(processing.get("check_after_secs") or 2))
        status = {"command": "STATUS", "media_id": media_id}
        polled = await client.get(url, params=status, headers={"Authorization": _oauth1_header("GET", url, creds, status)})
        if polled.status_code >= 400:
            raise RuntimeError(_error_text(polled))
        processing = polled.json().get("processing_info")
    raise RuntimeError("Twitter is still processing the video.")


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


async def _publish_instagram(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    if not creds.filled("meta_page_access_token", "instagram_business_account_id"):
        return _missing("instagram", "META_PAGE_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID")
    if content.video_bytes or content.video_url:
        if not is_public_http_url(content.video_url):
            return _failed("instagram", _public_media_message("إنستغرام"))
        fields = {"media_type": "REELS", "video_url": content.video_url, "caption": content.caption}
    elif content.image_bytes or content.image_url:
        if not is_public_http_url(content.image_url):
            return _failed("instagram", _public_media_message("إنستغرام"))
        fields = {"image_url": content.image_url, "caption": content.caption}
    else:
        return _unsupported("instagram", "إنستغرام يتطلب صورة أو فيديو. النص وحده غير مدعوم.")
    graph = f"https://graph.facebook.com/{creds.meta_graph_version.strip() or 'v21.0'}"
    account = creds.instagram_business_account_id.strip()
    token = creds.meta_page_access_token.strip()
    created = await client.post(f"{graph}/{account}/media", data={**fields, "access_token": token})
    if created.status_code >= 400:
        return _failed("instagram", _error_text(created))
    container = str(created.json().get("id") or "")
    for _ in range(15):
        status = await client.get(f"{graph}/{container}", params={"fields": "status_code", "access_token": token})
        code = (status.json().get("status_code") if status.status_code < 400 else "") or ""
        if code == "FINISHED":
            break
        if code == "ERROR":
            return _failed("instagram", _error_text(status))
        await _sleep(3)
    else:
        return _failed("instagram", "إنستغرام ما زال يعالج الوسائط. أعد المحاولة بعد لحظات.")
    published = await client.post(
        f"{graph}/{account}/media_publish", data={"creation_id": container, "access_token": token})
    if published.status_code >= 400:
        return _failed("instagram", _error_text(published))
    post_id = str(published.json().get("id") or container)
    return _published("instagram", post_id, f"https://www.instagram.com/")


async def _publish_youtube(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    ready = creds.filled("youtube_access_token") or creds.filled(
        "youtube_refresh_token", "google_client_id", "google_client_secret")
    if not ready:
        return _missing("youtube", "YOUTUBE_ACCESS_TOKEN أو YOUTUBE_REFRESH_TOKEN مع GOOGLE_CLIENT_ID و GOOGLE_CLIENT_SECRET")
    if not content.video_bytes:
        return _unsupported("youtube", "يوتيوب يتطلب ملف فيديو. النص أو الصورة وحدهما غير مدعومين.")
    try:
        token = await _youtube_token(client, creds)
    except RuntimeError as exc:
        return _failed("youtube", str(exc))
    if not token:
        return _failed("youtube", "تعذر الحصول على رمز يوتيوب من social.env.")
    privacy = creds.youtube_privacy_status.strip() or "public"
    if privacy not in {"public", "unlisted", "private"}:
        privacy = "public"
    metadata = {
        "snippet": {"title": content.title, "description": content.caption[:5000], "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    auth = {"Authorization": f"Bearer {token}"}
    opened = await client.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        json=metadata,
        headers={**auth, "X-Upload-Content-Type": content.video_mime or "video/mp4",
                 "X-Upload-Content-Length": str(len(content.video_bytes))},
    )
    if opened.status_code >= 400:
        return _failed("youtube", _error_text(opened))
    location = opened.headers.get("location")
    if not location:
        return _failed("youtube", "يوتيوب لم يعِد جلسة رفع.")
    uploaded = await client.put(
        location, content=content.video_bytes,
        headers={**auth, "Content-Type": content.video_mime or "video/mp4"},
    )
    if uploaded.status_code >= 400:
        return _failed("youtube", _error_text(uploaded))
    video_id = str(uploaded.json().get("id") or "")
    return _published("youtube", video_id, f"https://www.youtube.com/watch?v={video_id}" if video_id else None)


async def _publish_tiktok(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    if not creds.filled("tiktok_access_token"):
        return _missing("tiktok", "TIKTOK_ACCESS_TOKEN")
    token = creds.tiktok_access_token.strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    privacy = creds.tiktok_privacy_level.strip() or "SELF_ONLY"
    if content.video_bytes:
        return await _tiktok_video(client, headers, content, privacy)
    if content.image_url and is_public_http_url(content.image_url):
        response = await client.post("https://open.tiktokapis.com/v2/post/publish/content/init/", headers=headers, json={
            "post_info": {"title": content.caption[:2200], "privacy_level": privacy},
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": [content.image_url]},
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        })
        if response.status_code >= 400:
            return _failed("tiktok", _error_text(response))
        data = response.json()
        if (data.get("error") or {}).get("code") not in {None, "ok"}:
            return _failed("tiktok", str(data["error"].get("message") or data["error"])[:300])
        publish_id = str((data.get("data") or {}).get("publish_id") or "")
        return _published("tiktok", publish_id, message=_tiktok_note(privacy))
    if content.image_bytes:
        return _failed("tiktok", _public_media_message("تيك توك للصور"))
    return _unsupported("tiktok", "تيك توك يتطلب فيديو أو صورة على رابط عام.")


async def _tiktok_video(client: httpx.AsyncClient, headers: dict, content: HubContent, privacy: str) -> PlatformResult:
    video = content.video_bytes or b""
    chunk = len(video) if len(video) <= 10 * 1024 * 1024 else 10 * 1024 * 1024
    total = 1 if len(video) <= chunk else max(1, len(video) // chunk)
    if total == 1:
        chunk = len(video)
    response = await client.post("https://open.tiktokapis.com/v2/post/publish/video/init/", headers=headers, json={
        "post_info": {
            "title": content.caption[:2200], "privacy_level": privacy,
            "disable_duet": False, "disable_comment": False, "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD", "video_size": len(video), "chunk_size": chunk, "total_chunk_count": total,
        },
    })
    if response.status_code >= 400:
        return _failed("tiktok", _error_text(response))
    data = response.json()
    error = data.get("error") or {}
    if error.get("code") not in {None, "ok"}:
        return _failed("tiktok", str(error.get("message") or error)[:300])
    publish_id = str(data["data"]["publish_id"])
    upload_url = data["data"]["upload_url"]
    for index in range(total):
        start = index * chunk
        end = len(video) - 1 if index == total - 1 else start + chunk - 1
        part = await client.put(
            upload_url, content=video[start:end + 1],
            headers={"Content-Type": content.video_mime or "video/mp4", "Content-Range": f"bytes {start}-{end}/{len(video)}"},
        )
        if part.status_code >= 400:
            return _failed("tiktok", _error_text(part))
    return _published("tiktok", publish_id, message=_tiktok_note(privacy))


def _tiktok_note(privacy: str) -> str:
    if privacy == "SELF_ONLY":
        return "تم الإرسال. التطبيقات غير المراجعة على تيك توك تنشر بشكل خاص فقط (SELF_ONLY)."
    return "تم النشر"


async def _publish_pinterest(content: HubContent, creds: SocialCredentials, client: httpx.AsyncClient) -> PlatformResult:
    if not creds.filled("pinterest_access_token", "pinterest_board_id"):
        return _missing("pinterest", "PINTEREST_ACCESS_TOKEN, PINTEREST_BOARD_ID")
    headers = {"Authorization": f"Bearer {creds.pinterest_access_token.strip()}"}
    description = content.caption[:800]
    title = content.title
    if content.image_bytes and not content.video_bytes:
        source = {
            "source_type": "image_base64",
            "content_type": content.image_mime or "image/jpeg",
            "data": base64.b64encode(content.image_bytes).decode(),
        }
    elif content.video_bytes:
        media_id = await _pinterest_video(client, headers, content.video_bytes, content.video_mime or "video/mp4")
        source = {"source_type": "video_id", "media_id": media_id, "cover_image_key_frame_time": 0}
    elif is_public_http_url(content.image_url):
        source = {"source_type": "image_url", "url": content.image_url}
    else:
        return _unsupported("pinterest", "بنترست يتطلب صورة أو فيديو.")
    response = await client.post("https://api.pinterest.com/v5/pins", headers=headers, json={
        "board_id": creds.pinterest_board_id.strip(),
        "title": title,
        "description": description,
        "media_source": source,
    })
    if response.status_code >= 400:
        return _failed("pinterest", _error_text(response))
    pin = response.json()
    pin_id = str(pin.get("id") or "")
    return _published("pinterest", pin_id, pin.get("link") or (f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else None))


async def _pinterest_video(client: httpx.AsyncClient, headers: dict, data: bytes, mime: str) -> str:
    registered = await client.post("https://api.pinterest.com/v5/media", headers=headers, json={"media_type": "video"})
    if registered.status_code >= 400:
        raise RuntimeError(_error_text(registered))
    body = registered.json()
    media_id = str(body["media_id"])
    upload = body.get("upload_url")
    fields = dict(body.get("upload_parameters") or {})
    if not upload:
        raise RuntimeError("Pinterest did not return an upload URL.")
    sent = await client.post(upload, data=fields, files={"file": ("video", data, mime)})
    if sent.status_code >= 400:
        raise RuntimeError(_error_text(sent))
    for _ in range(15):
        status = await client.get(f"https://api.pinterest.com/v5/media/{media_id}", headers=headers)
        if status.status_code >= 400:
            raise RuntimeError(_error_text(status))
        state = str(status.json().get("status") or "")
        if state == "succeeded":
            return media_id
        if state == "failed":
            raise RuntimeError("Pinterest failed to process the video.")
        await _sleep(2)
    raise RuntimeError("Pinterest is still processing the video.")


SNAPCHAT_API = "https://businessapi.snapchat.com"
_SNAPCHAT_CHUNK = 32 * 1024 * 1024


def _snapchat_token_message() -> str:
    return "أضف SNAPCHAT_CLIENT_ID و SNAPCHAT_ACCESS_TOKEN و SNAPCHAT_PROFILE_ID في social.env."


# Fresh access tokens live only in this process. social.env keeps the long-lived OAuth values.
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
    """Client id, client secret, and the long-lived refresh token from social.env."""
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
        return {"ok": False, "message": "أضف SNAPCHAT_PROFILE_ID في social.env"}
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


_PUBLISHERS = {
    "twitter": _publish_twitter,
    "linkedin": _publish_linkedin,
    "instagram": _publish_instagram,
    "youtube": _publish_youtube,
    "tiktok": _publish_tiktok,
    "pinterest": _publish_pinterest,
    "snapchat": _publish_snapchat,
}


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


async def publish_everywhere(platforms: list[str], content: HubContent, creds: SocialCredentials) -> list[PlatformResult]:
    timeout = httpx.Timeout(60.0, read=180.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results: list[PlatformResult] = []
        for platform in platforms:
            publisher = _PUBLISHERS.get(platform)
            if publisher is None:
                results.append(_failed(platform, "منصة غير معروفة."))
                continue
            try:
                results.append(await publisher(content, creds, client))
            except Exception as exc:  # surface provider failures per network, keep the others going
                results.append(_failed(platform, str(exc) or "تعذر النشر"))
        return results
