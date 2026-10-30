"""News and blog posts written by the admin, stored in Postgres.

Cover images are kept in the database rather than on disk. This backend runs on a read-only
filesystem in production (serverless), so an uploaded file would not survive — and the existing
`LocalStorage` backend only works in development. Cover images are small and few, so a `BYTEA`
column is the portable choice; they are served back through a dedicated endpoint with caching.

Images are validated by content (magic bytes plus a Pillow decode), never by the MIME type the
browser claims. Schema creation follows the `CREATE TABLE IF NOT EXISTS` convention used by the
reviews and news-read modules in this project.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.core.exceptions import NotFoundError, UnprocessableError
from app.core.logging import get_logger

logger = get_logger(__name__)

STATUSES = ("draft", "published")
SOURCE_NAME = "نثيل"

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MIN_DIMENSION = 100
MAX_DIMENSION = 8000
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

TITLE_MAX = 200
SUMMARY_MAX = 500
BODY_MAX = 50_000

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS news_posts (
        id UUID PRIMARY KEY,
        title TEXT NOT NULL,
        summary TEXT,
        body TEXT NOT NULL,
        image_data BYTEA,
        image_mime TEXT,
        image_name TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
        published_at TIMESTAMPTZ,
        author_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_news_posts_status_date ON news_posts (status, published_at DESC)",
)

_schema_ready = False


@dataclass(slots=True)
class ImageUpload:
    data: bytes
    mime: str
    name: str | None


async def _ensure_schema(session) -> None:
    global _schema_ready
    if _schema_ready:
        return
    for statement in SCHEMA_STATEMENTS:
        await session.execute(text(statement))
    await session.commit()
    _schema_ready = True


# --------------------------------------------------------------------------- validation


def _sniff(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, filename: str | None) -> ImageUpload:
    """Accept only a real JPEG/PNG/WEBP, judged by its bytes rather than its declared type."""
    if not data:
        raise UnprocessableError("ملف الصورة فارغ.", code="IMAGE_EMPTY")
    if len(data) > MAX_IMAGE_BYTES:
        raise UnprocessableError(
            f"حجم الصورة أكبر من الحد المسموح ({MAX_IMAGE_BYTES // (1024 * 1024)} ميجابايت).",
            code="IMAGE_TOO_LARGE",
        )
    mime = _sniff(data[:16])
    if mime not in ALLOWED_IMAGE_MIMES:
        raise UnprocessableError("صيغة الصورة غير مدعومة. استخدم JPG أو PNG أو WEBP.",
                                 code="IMAGE_TYPE_UNSUPPORTED")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()  # structural check; the file is re-opened below for its size
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UnprocessableError("تعذر قراءة ملف الصورة.", code="IMAGE_UNREADABLE") from exc

    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise UnprocessableError(f"أبعاد الصورة صغيرة جداً (الحد الأدنى {MIN_DIMENSION} بكسل).",
                                 code="IMAGE_TOO_SMALL")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise UnprocessableError(f"أبعاد الصورة كبيرة جداً (الحد الأقصى {MAX_DIMENSION} بكسل).",
                                 code="IMAGE_TOO_LARGE")
    return ImageUpload(data=data, mime=mime, name=(filename or "").strip()[:200] or None)


def _clean_text(value: Any, *, field: str, maximum: int, required: bool) -> str | None:
    text_value = (value or "").strip() if isinstance(value, str) else ""
    if not text_value:
        if required:
            raise UnprocessableError(f"الحقل {field} مطلوب.", code="FIELD_REQUIRED")
        return None
    if len(text_value) > maximum:
        raise UnprocessableError(f"الحقل {field} أطول من الحد المسموح ({maximum} حرفاً).",
                                 code="FIELD_TOO_LONG")
    return text_value


def _publish_moment(published_at: Any, status: str) -> datetime | None:
    """The admin picks the publication date; an empty one on a published post means now."""
    if isinstance(published_at, datetime):
        moment = published_at
    elif isinstance(published_at, date):
        moment = datetime(published_at.year, published_at.month, published_at.day)
    elif isinstance(published_at, str) and published_at.strip():
        try:
            moment = datetime.fromisoformat(published_at.strip().replace("Z", "+00:00"))
        except ValueError:
            raise UnprocessableError("تاريخ النشر غير صالح.", code="INVALID_DATE") from None
    else:
        return datetime.now(timezone.utc) if status == "published" else None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- serialisation


def _row_to_post(row, *, include_body: bool = True) -> dict[str, Any]:
    post = {
        "id": str(row.id),
        "origin": "natheel",
        "title": row.title,
        "summary": row.summary,
        "status": row.status,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "has_image": bool(row.image_mime),
        "image_url": f"/api/public/news/posts/{row.id}/image" if row.image_mime else None,
        "source": {"name": SOURCE_NAME, "url": None},
    }
    if include_body:
        post["body"] = row.body
    return post


# --------------------------------------------------------------------------- queries

_COLUMNS = ("id, title, summary, body, image_mime, status, published_at, created_at, updated_at")


async def list_posts(
    *, status: str | None = None, limit: int = 20, offset: int = 0, with_body: bool = False
) -> dict[str, Any]:
    """Newest first. Drafts have no publication date, so they sort by creation instead."""
    where = "WHERE status = :status" if status in STATUSES else ""
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM news_posts {where}"),
                {"status": status} if where else {},
            )
        ).scalar() or 0
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM news_posts {where}
                    ORDER BY COALESCE(published_at, created_at) DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**({"status": status} if where else {}), "limit": limit, "offset": offset},
            )
        ).all()

    items = [_row_to_post(row, include_body=with_body) for row in rows]
    return {"items": items, "total": int(total), "limit": limit, "offset": offset,
            "has_more": offset + len(items) < int(total)}


async def get_post(post_id: str, *, published_only: bool = False) -> dict[str, Any] | None:
    try:
        identifier = UUID(str(post_id))
    except (ValueError, AttributeError, TypeError):
        return None
    clause = " AND status = 'published'" if published_only else ""
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        row = (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM news_posts WHERE id = :id{clause}"),
                {"id": identifier},
            )
        ).first()
    return _row_to_post(row) if row else None


async def get_image(post_id: str) -> tuple[bytes, str] | None:
    try:
        identifier = UUID(str(post_id))
    except (ValueError, AttributeError, TypeError):
        return None
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        row = (
            await session.execute(
                text("SELECT image_data, image_mime FROM news_posts WHERE id = :id"),
                {"id": identifier},
            )
        ).first()
    if row is None or not row.image_data or not row.image_mime:
        return None
    return bytes(row.image_data), row.image_mime


# --------------------------------------------------------------------------- mutations


async def create_post(
    *,
    title: str,
    body: str,
    summary: str | None,
    status: str,
    published_at: Any,
    image: ImageUpload | None,
    author_id: str | None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise UnprocessableError("حالة النشر غير صالحة.", code="INVALID_STATUS")
    clean_title = _clean_text(title, field="العنوان", maximum=TITLE_MAX, required=True)
    clean_body = _clean_text(body, field="النص", maximum=BODY_MAX, required=True)
    clean_summary = _clean_text(summary, field="الملخص", maximum=SUMMARY_MAX, required=False)
    moment = _publish_moment(published_at, status)
    post_id = uuid4()

    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text(
                """
                INSERT INTO news_posts (id, title, summary, body, image_data, image_mime,
                                        image_name, status, published_at, author_id)
                VALUES (:id, :title, :summary, :body, :image_data, :image_mime,
                        :image_name, :status, :published_at, :author_id)
                """
            ),
            {
                "id": post_id, "title": clean_title, "summary": clean_summary, "body": clean_body,
                "image_data": image.data if image else None,
                "image_mime": image.mime if image else None,
                "image_name": image.name if image else None,
                "status": status, "published_at": moment, "author_id": author_id,
            },
        )
        await session.commit()
    logger.info("news post created", extra={"status": status})
    return await get_post(str(post_id))  # type: ignore[return-value]


async def update_post(
    post_id: str,
    *,
    title: Any = None,
    body: Any = None,
    summary: Any = None,
    status: Any = None,
    published_at: Any = None,
    image: ImageUpload | None = None,
    remove_image: bool = False,
) -> dict[str, Any]:
    """Only the fields actually supplied are touched, so a partial edit cannot blank the rest."""
    existing = await get_post(post_id)
    if existing is None:
        raise NotFoundError("الخبر غير موجود.", code="NEWS_POST_NOT_FOUND")

    updates: dict[str, Any] = {}
    if title is not None:
        updates["title"] = _clean_text(title, field="العنوان", maximum=TITLE_MAX, required=True)
    if body is not None:
        updates["body"] = _clean_text(body, field="النص", maximum=BODY_MAX, required=True)
    if summary is not None:
        updates["summary"] = _clean_text(summary, field="الملخص", maximum=SUMMARY_MAX, required=False)

    next_status = existing["status"]
    if status is not None:
        if status not in STATUSES:
            raise UnprocessableError("حالة النشر غير صالحة.", code="INVALID_STATUS")
        next_status = status
        updates["status"] = status

    if published_at is not None or status is not None:
        supplied = published_at if published_at is not None else existing["published_at"]
        # Publishing something that never had a date stamps it now.
        if next_status == "published" and not supplied:
            supplied = None
        updates["published_at"] = _publish_moment(supplied, next_status)

    if remove_image:
        updates.update({"image_data": None, "image_mime": None, "image_name": None})
    elif image is not None:
        updates.update({"image_data": image.data, "image_mime": image.mime, "image_name": image.name})

    if not updates:
        return existing

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text(f"UPDATE news_posts SET {assignments}, updated_at = NOW() WHERE id = :id"),
            {**updates, "id": UUID(post_id)},
        )
        await session.commit()
    return await get_post(post_id)  # type: ignore[return-value]


async def delete_post(post_id: str) -> bool:
    try:
        identifier = UUID(str(post_id))
    except (ValueError, AttributeError, TypeError):
        return False
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(
            text("DELETE FROM news_posts WHERE id = :id"), {"id": identifier}
        )
        await session.commit()
    return bool(result.rowcount)
