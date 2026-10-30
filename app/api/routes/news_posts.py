"""Natheel's own news/blog posts: admin CRUD, plus the public feed the website reads.

Two routers, mounted under different prefixes so neither can be captured by the Ministry feed's
`/api/news/{key}` route:

    /api/news-posts      admin only - create, edit, publish, delete
    /api/public/news     unauthenticated - what visitors see

The public side deliberately exposes only published posts, never drafts, and never the body of a
draft through any field.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Path, Query, Response, UploadFile

from app.api.routes.reviews import require_admin_token
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, UnprocessableError
from app.news import posts, source

MINISTRY_DOWN = "تعذر تحميل أخبار وزارة السياحة حالياً."


def _news_admin(authorization: str | None = Header(default=None)) -> str:
    return require_admin_token(authorization)


router = APIRouter(prefix="/api/news-posts", tags=["Natheel News"],
                   dependencies=[Depends(_news_admin)])
public_router = APIRouter(prefix="/api/public/news", tags=["Natheel News (public)"])


async def _read_image(upload: UploadFile | None) -> posts.ImageUpload | None:
    if upload is None or not upload.filename:
        return None
    data = await upload.read()
    if not data:
        return None
    return posts.validate_image(data, upload.filename)


# --------------------------------------------------------------------------- admin


@router.get("", summary="List Natheel's posts, drafts included")
async def list_own_posts(
    status: str | None = Query(default=None, pattern="^(draft|published)$"),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return await posts.list_posts(status=status, limit=limit, offset=offset)


@router.post("", status_code=201, summary="Create a post (multipart: fields + optional cover image)")
async def create_own_post(
    admin_id: str = Depends(_news_admin),
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    summary: Annotated[str | None, Form()] = None,
    status: Annotated[str, Form()] = "draft",
    published_at: Annotated[str | None, Form()] = None,
    image: Annotated[UploadFile | None, File()] = None,
) -> dict:
    return await posts.create_post(
        title=title, body=body, summary=summary, status=status,
        published_at=published_at, image=await _read_image(image), author_id=admin_id,
    )


@router.get("/{post_id}", summary="One post, draft or published")
async def get_own_post(post_id: str = Path(min_length=1, max_length=64)) -> dict:
    post = await posts.get_post(post_id)
    if post is None:
        raise NotFoundError("الخبر غير موجود.", code="NEWS_POST_NOT_FOUND")
    return post


@router.patch("/{post_id}", summary="Edit a post; omitted fields are left as they are")
async def update_own_post(
    post_id: str = Path(min_length=1, max_length=64),
    title: Annotated[str | None, Form()] = None,
    body: Annotated[str | None, Form()] = None,
    summary: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
    published_at: Annotated[str | None, Form()] = None,
    remove_image: Annotated[bool, Form()] = False,
    image: Annotated[UploadFile | None, File()] = None,
) -> dict:
    return await posts.update_post(
        post_id, title=title, body=body, summary=summary, status=status,
        published_at=published_at, image=await _read_image(image), remove_image=remove_image,
    )


@router.delete("/{post_id}", summary="Delete a post permanently")
async def delete_own_post(post_id: str = Path(min_length=1, max_length=64)) -> dict:
    if not await posts.delete_post(post_id):
        raise NotFoundError("الخبر غير موجود.", code="NEWS_POST_NOT_FOUND")
    return {"id": post_id, "deleted": True}


# --------------------------------------------------------------------------- public


@public_router.get("/posts", summary="Published Natheel posts")
async def public_posts(
    limit: int = Query(default=12, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return await posts.list_posts(status="published", limit=limit, offset=offset)


# Declared before "/posts/{post_id}" so the literal path is not read as an id.
@public_router.get("/ministry", summary="Ministry of Tourism news, for the public website")
async def public_ministry(
    limit: int = Query(default=12, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    locale: str = Query(default=source.LOCALE, pattern="^(ar|en)$"),
) -> dict:
    """The same cached Ministry feed the admin page uses, minus anything admin-specific.

    Read/unread state is per-admin and never leaves the admin area, so it is not included here.
    """
    try:
        feed = await source.fetch_all_linked(locale)
    except source.NewsUnavailable:
        # The website should still render its own posts if the Ministry is unreachable.
        return {"items": [], "total": 0, "limit": limit, "offset": offset,
                "has_more": False, "unavailable": True, "synced_at": None}

    articles = feed.get("articles") or []
    window = articles[offset : offset + limit]
    return {
        "items": [{**source.as_list_item(article), "origin": "ministry"} for article in window],
        "total": len(articles),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < len(articles),
        "unavailable": False,
        "synced_at": feed.get("fetched_at"),
    }


@public_router.get("/posts/{post_id}", summary="One published Natheel post")
async def public_post(post_id: str = Path(min_length=1, max_length=64)) -> dict:
    post = await posts.get_post(post_id, published_only=True)
    if post is None:
        raise NotFoundError("الخبر غير موجود.", code="NEWS_POST_NOT_FOUND")
    return post


@public_router.get("/posts/{post_id}/image", summary="Cover image bytes for a post")
async def public_post_image(post_id: str = Path(min_length=1, max_length=64)) -> Response:
    found = await posts.get_image(post_id)
    if found is None:
        raise NotFoundError("لا توجد صورة لهذا الخبر.", code="NEWS_POST_IMAGE_NOT_FOUND")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        headers={
            # Immutable for a day: editing a post replaces the row, and the admin view bypasses
            # the cache with a version query string.
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
