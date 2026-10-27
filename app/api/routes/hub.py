"""Admin Social Media Hub: create a post and share it to the connected networks.

Nothing is ever saved on this server: there is no local database table or JSON/media file
for hub posts, by design — this backend runs on a read-only filesystem in production
(serverless), and more importantly Zoho Social is the single source of truth. Every
published post lives only in Zoho (for the networks it manages) or directly on LinkedIn/
Snapchat's own servers; `GET /posts` always reads the live feed, never a local copy."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from app.api.routes.reviews import require_admin_token
from app.core.config import get_settings
from app.hub import zoho_social
from app.hub.analytics import analytics_history, sync_social_analytics
from app.hub.credentials import get_social_credentials
from app.hub.publish import PLATFORMS, HubContent, is_public_http_url, publish_everywhere, snapchat_account_check

def _hub_admin(authorization: str | None = Header(default=None)) -> str:
    return require_admin_token(authorization)


router = APIRouter(
    prefix="/api/hub",
    tags=["Social Media Hub"],
    dependencies=[Depends(_hub_admin)],
)


async def _read_upload(upload: UploadFile | None, limit: int, kind: str) -> tuple[bytes, str] | None:
    if upload is None or not upload.filename:
        return None
    data = await upload.read()
    if not data:
        return None
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"حجم {kind} أكبر من الحد المسموح.")
    mime = (upload.content_type or "application/octet-stream").split(";")[0].strip().lower()
    return data, mime


async def _download(url: str, limit: int) -> tuple[bytes, str]:
    timeout = httpx.Timeout(60.0, read=180.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise HTTPException(status_code=400, detail="تعذر تنزيل رابط الفيديو.")
            mime = (response.headers.get("content-type") or "video/mp4").split(";")[0].strip().lower()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail="حجم الفيديو أكبر من الحد المسموح.")
                chunks.append(chunk)
    return b"".join(chunks), mime


@router.get("/status", summary="Which networks have credentials in .env")
async def credential_status() -> dict:
    creds = get_social_credentials()
    ready = creds.platform_ready()
    details: dict = {}
    if creds.filled("snapchat_profile_id") or creds.filled("snapchat_client_id") or creds.filled("snapchat_access_token"):
        details["snapchat"] = await snapchat_account_check(creds)
    if creds.zoho_ready():
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            details["zoho_social"] = await zoho_social.channel_check(client, creds)
    return {
        "credentials_file": ".env",
        "platforms": ready,
        "zoho_managed_platforms": sorted(zoho_social.managed_platforms(creds)),
        "details": details,
    }


@router.get("/zoho/channels", summary="Social accounts already connected inside Zoho Social")
async def zoho_channels() -> dict:
    """Live list from Zoho (not from .env): every channel the connected Zoho Social
    portal/brand already has linked, so the admin can see what will actually receive
    a post without opening the Zoho Social console."""
    creds = get_social_credentials()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        return await zoho_social.channel_check(client, creds)


@router.get("/posts", summary="Recent posts, live from Zoho Social")
async def posts() -> dict:
    """Zoho Social's own live "Published Posts" feed — every network it manages for this
    brand, even posts made outside this tool. Nothing is stored locally; LinkedIn/Snapchat
    posts (the only two networks not routed through Zoho) are returned once, directly in
    the `POST /posts` response at publish time, but have no history endpoint of their own."""
    creds = get_social_credentials()
    if not creds.zoho_ready():
        return {"posts": []}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        try:
            zoho_posts = await zoho_social.recent_posts(client, creds)
        except Exception:
            zoho_posts = []
    return {"posts": zoho_posts}


@router.get("/analytics", summary="Fetch real metrics from connected social providers")
async def analytics() -> dict:
    return await sync_social_analytics()


@router.post("/analytics/sync", summary="Refresh and persist real social metrics")
async def refresh_analytics() -> dict:
    return await sync_social_analytics()


@router.get("/analytics/history", summary="Timestamped real social metric snapshots")
async def metric_history() -> dict:
    return {"history": await analytics_history()}


@router.delete("/posts/{post_id}", summary="Delete a platform post record")
async def remove_post(post_id: str) -> dict:
    """Zoho Social has no API to delete an already-published post (only drafts/schedules),
    and this server keeps no local copy to delete either — every post is sourced live from
    Zoho or returned once at publish time. Open the post's real network/Zoho link to delete
    it manually from there (the frontend already does this instead of calling this route)."""
    raise HTTPException(
        status_code=400,
        detail="Zoho Social لا يوفر واجهة لحذف منشور نُشر فعلاً، ولا يحتفظ هذا الخادم بنسخة "
               "محلية لحذفها. افتح رابط المنشور على الشبكة أو على Zoho لحذفه من هناك.",
    )


@router.post("/posts", summary="Create a post and share it to the selected networks")
async def create_post(
    caption: str = Form(""),
    platforms: str = Form(""),
    video_url: str = Form(""),
    image: UploadFile | None = File(None),
    video: UploadFile | None = File(None),
) -> dict:
    settings = get_settings()
    creds = get_social_credentials()
    selected = [item.strip().lower() for item in platforms.split(",") if item.strip()]
    unknown = [item for item in selected if item not in PLATFORMS]
    if not selected:
        raise HTTPException(status_code=400, detail="حدد منصة واحدة على الأقل.")
    if unknown:
        raise HTTPException(status_code=400, detail=f"منصات غير معروفة: {', '.join(unknown)}")
    text = caption.strip()
    if len(text) > 2200:
        raise HTTPException(status_code=400, detail="نص المنشور أطول من 2200 حرف.")

    image_data = await _read_upload(image, settings.max_image_bytes, "الصورة")
    video_data = await _read_upload(video, settings.max_video_bytes, "الفيديو")
    remote_video = video_url.strip()
    if image_data and not image_data[1].startswith("image/"):
        raise HTTPException(status_code=400, detail="الملف المرفق ليس صورة.")
    if video_data and not video_data[1].startswith("video/"):
        raise HTTPException(status_code=400, detail="الملف المرفق ليس فيديو.")
    if remote_video and not (remote_video.startswith("https://") or remote_video.startswith("http://")):
        raise HTTPException(status_code=400, detail="رابط الفيديو غير صالح.")
    if not text and not image_data and not video_data and not remote_video:
        raise HTTPException(status_code=400, detail="أضف نصاً أو صورة أو فيديو.")

    video_mime = video_data[1] if video_data else None
    video_bytes = video_data[0] if video_data else None
    if remote_video and video_bytes is None:
        # Downloaded into memory only — LinkedIn/Snapchat need raw bytes to upload a video
        # directly to their own API; nothing is ever written to disk.
        try:
            video_bytes, video_mime = await _download(remote_video, settings.max_video_bytes)
        except Exception:
            video_bytes = None
            video_mime = "video/mp4"

    # Zoho Social's API has no endpoint to upload a video file directly (only images) — a
    # video destined for a Zoho-managed platform must already be a public HTTPS URL that
    # Zoho can fetch itself. There is no local disk to host an uploaded file on either way.
    zoho_targets = [p for p in selected if p in zoho_social.managed_platforms(creds)]
    video_file_for_zoho = zoho_targets and (video_data or (remote_video and not is_public_http_url(remote_video)))
    if video_file_for_zoho:
        raise HTTPException(
            status_code=400,
            detail="فيديو منصات Zoho Social (" + "، ".join(zoho_targets) + ") يجب أن يكون رابط HTTPS "
            "عاماً — رفع ملف فيديو مباشرة غير مدعوم لهذه المنصات. الصق رابط فيديو عاماً، أو ألغِ "
            "تحديد هذه المنصات ووجّه الفيديو إلى لينكد إن/سناب شات فقط.",
        )

    content = HubContent(
        caption=text,
        image_bytes=image_data[0] if image_data else None,
        image_mime=image_data[1] if image_data else None,
        video_bytes=video_bytes,
        video_mime=video_mime,
        video_url=remote_video or None,
    )
    results = await publish_everywhere(selected, content, creds)
    published = [item.platform for item in results if item.status == "published"]
    if published and len(published) == len(results):
        status = "published"
    elif published:
        status = "partial"
    else:
        status = "saved"

    # Nothing below is persisted anywhere: this is handed straight back to the admin UI,
    # which adds it to the on-screen list itself. The real, durable record of this post is
    # whatever `results` says got published — on Zoho Social, LinkedIn, and/or Snapchat.
    media_type = "video" if (video_bytes or remote_video) else "image" if image_data else "text"
    post = {
        "id": uuid4().hex,
        "caption": text or "منشور جديد من شركة نثيل",
        "media_type": media_type,
        "platforms": selected,
        "status": status,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "results": [item.as_dict() for item in results],
    }
    return {"success": True, "post": post, "results": post["results"]}
