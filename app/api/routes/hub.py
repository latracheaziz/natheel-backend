"""Admin Social Media Hub: create a post on our platform and share it."""
from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile

from app.api.routes.reviews import require_admin_token
from app.core.config import get_settings
from app.hub.analytics import analytics_history, sync_social_analytics
from app.hub.credentials import CREDENTIALS_FILE, get_social_credentials
from app.hub.publish import PLATFORMS, HubContent, is_public_http_url, publish_everywhere, snapchat_account_check
from app.hub.store import delete_post, list_posts, save_post

def _hub_admin(authorization: str | None = Header(default=None)) -> str:
    return require_admin_token(authorization)


router = APIRouter(
    prefix="/api/hub",
    tags=["Social Media Hub"],
    dependencies=[Depends(_hub_admin)],
)


def _suffix(filename: str | None, mime: str | None, fallback: str) -> str:
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        if 1 < len(ext) <= 8:
            return ext
    guessed = mimetypes.guess_extension(mime or "") or ""
    return guessed or fallback


def _media_root():
    from pathlib import Path

    root = Path(get_settings().storage_local_path)
    if not root.is_absolute():
        root = CREDENTIALS_FILE.parent / root
    folder = root / "hub"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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


def _public_url(request: Request, key: str) -> str:
    creds = get_social_credentials()
    settings = get_settings()
    base = (creds.media_public_base_url or settings.media_public_base_url or "").strip()
    if not base:
        base = str(request.base_url).rstrip("/") + "/media-files"
    return f"{base.rstrip('/')}/{key}"


@router.get("/status", summary="Which networks have credentials in .env")
async def credential_status() -> dict:
    creds = get_social_credentials()
    ready = creds.platform_ready()
    details: dict = {}
    if creds.filled("snapchat_profile_id") or creds.filled("snapchat_client_id") or creds.filled("snapchat_access_token"):
        details["snapchat"] = await snapchat_account_check(creds)
    return {
        "credentials_file": ".env",
        "platforms": ready,
        "details": details,
    }


@router.get("/posts", summary="Posts created on this platform")
async def posts() -> dict:
    return {"posts": list_posts()}


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
    if not delete_post(post_id):
        raise HTTPException(status_code=404, detail="المنشور غير موجود.")
    return {"success": True, "id": post_id}


@router.post("/posts", summary="Create a post and share it to the selected networks")
async def create_post(
    request: Request,
    caption: str = Form(""),
    platforms: str = Form(""),
    video_url: str = Form(""),
    image: UploadFile | None = File(None),
    video: UploadFile | None = File(None),
) -> dict:
    settings = get_settings()
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
        try:
            video_bytes, video_mime = await _download(remote_video, settings.max_video_bytes)
        except Exception:
            video_bytes = None
            video_mime = "video/mp4"

    folder = _media_root()
    image_key = None
    video_key = None
    if image_data:
        image_key = f"hub/{uuid4().hex}{_suffix(image.filename if image else None, image_data[1], '.jpg')}"
        (folder.parent / image_key).parent.mkdir(parents=True, exist_ok=True)
        (folder.parent / image_key).write_bytes(image_data[0])
    if video_bytes and not remote_video:
        video_key = f"hub/{uuid4().hex}{_suffix(video.filename if video else None, video_mime, '.mp4')}"
        (folder.parent / video_key).write_bytes(video_bytes)
    elif video_bytes and remote_video and not is_public_http_url(remote_video):
        video_key = f"hub/{uuid4().hex}.mp4"
        (folder.parent / video_key).write_bytes(video_bytes)

    image_url = _public_url(request, image_key) if image_key else None
    saved_video_url = _public_url(request, video_key) if video_key else None
    share_video_url = remote_video if is_public_http_url(remote_video) else saved_video_url

    content = HubContent(
        caption=text,
        image_bytes=image_data[0] if image_data else None,
        image_mime=image_data[1] if image_data else None,
        image_url=image_url,
        video_bytes=video_bytes,
        video_mime=video_mime,
        video_url=share_video_url or (remote_video or None),
    )
    results = await publish_everywhere(selected, content, get_social_credentials())
    published = [item.platform for item in results if item.status == "published"]
    if published and len(published) == len(results):
        status = "published"
    elif published:
        status = "partial"
    else:
        status = "saved"

    media_type = "video" if video_bytes or remote_video else "image" if image_data else "text"
    post = {
        "id": uuid4().hex,
        "caption": text or "منشور جديد من شركة نثيل",
        "media_type": media_type,
        "media_url": image_url or "",
        "video_url": share_video_url or remote_video or "",
        "platforms": selected,
        "status": status,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "results": [item.as_dict() for item in results],
    }
    save_post(post)
    return {"success": True, "post": post, "results": post["results"]}
