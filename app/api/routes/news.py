"""Admin access to Ministry of Tourism news (source: app/news/source.py).

Reading the feed is read-only; the only thing this API writes is each admin's own read/unread
state (app/news/reads.py).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel

from app.api.routes.reviews import require_admin_token
from app.core.config import get_settings
from app.news import reads, service, source

SOURCE_DOWN = "تعذر تحميل الأخبار من موقع وزارة السياحة. حاول مرة أخرى لاحقاً."


def _news_admin(authorization: str | None = Header(default=None)) -> str:
    return require_admin_token(authorization)


router = APIRouter(
    prefix="/api/news",
    tags=["News"],
    dependencies=[Depends(_news_admin)],
)


class ReadState(BaseModel):
    read: bool = True


def _new_days() -> int:
    return get_settings().news_new_days


@router.get("", summary="Browse Ministry of Tourism news with filters, search and counters")
async def list_news(
    admin_id: str = Depends(_news_admin),
    status: str = Query(default="all", pattern="^(all|new|unread|read)$"),
    q: str | None = Query(default=None, max_length=200),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=500),
    page_size: int = Query(default=12, ge=1, le=50),
    locale: str = Query(default=source.LOCALE, pattern="^(ar|en)$"),
    refresh: bool = Query(default=False, description="Re-read the source now instead of using the cached feed"),
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="تاريخ البداية يجب أن يكون قبل تاريخ النهاية.")
    try:
        return await service.browse(
            admin_id,
            status=status,
            query=q,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
            locale=locale,
            new_days=_new_days(),
            force=refresh,
        )
    except source.NewsUnavailable as exc:
        raise HTTPException(status_code=502, detail=SOURCE_DOWN) from exc


# Declared before "/{key}" so the literal path is not captured as an article key.
@router.get("/counters", summary="New / unread / read totals for the admin navigation badge")
async def news_counters(
    admin_id: str = Depends(_news_admin),
    locale: str = Query(default=source.LOCALE, pattern="^(ar|en)$"),
    refresh: bool = Query(default=False, description="Re-read the source now instead of using the cached feed"),
) -> dict:
    try:
        return await service.counters(admin_id, locale=locale, new_days=_new_days(), force=refresh)
    except source.NewsUnavailable as exc:
        raise HTTPException(status_code=502, detail=SOURCE_DOWN) from exc


@router.get("/{key}", summary="One article in full, with any translations the Ministry published")
async def get_news(
    key: str = Path(min_length=1, max_length=200),
    locale: str = Query(default=source.LOCALE, pattern="^(ar|en)$"),
) -> dict:
    try:
        article = await source.get_article(key, locale)
    except source.NewsUnavailable as exc:
        raise HTTPException(status_code=502, detail=SOURCE_DOWN) from exc
    if article is None:
        raise HTTPException(status_code=404, detail="الخبر غير موجود.")
    return article


@router.post("/{key}/read", summary="Mark one article read or unread for the signed-in admin")
async def set_read_state(
    body: ReadState,
    admin_id: str = Depends(_news_admin),
    key: str = Path(min_length=1, max_length=200),
) -> dict:
    stored = await reads.set_read(admin_id, key, body.read)
    return {"id": key, "is_read": stored}
