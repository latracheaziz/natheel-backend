"""Public review submission and admin moderation."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.reviews.security import issue_admin_token, read_admin_token, verify_password

router = APIRouter(tags=["Reviews"])

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS admins (
        id UUID PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id UUID PRIMARY KEY,
        author_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'عميل',
        comment TEXT NOT NULL,
        rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


class ReviewCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    comment: str = Field(min_length=2, max_length=500)
    rating: int = Field(ge=1, le=5)
    role: str = Field(default="عميل", max_length=120)


class ReviewDecision(BaseModel):
    status: str = Field(pattern="^(accepted|declined)$")


class AdminLogin(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _review(row) -> dict:
    created: datetime = row.created_at
    return {
        "id": str(row.id),
        "name": row.author_name,
        "role": row.role,
        "comment": row.comment,
        "rating": row.rating,
        "status": row.status,
        "created_at": created.isoformat(),
    }


async def _ensure_schema(session) -> None:
    for statement in SCHEMA_STATEMENTS:
        await session.execute(text(statement))
    await session.commit()


def require_admin_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="يلزم تسجيل الدخول كمدير.")
    admin_id = read_admin_token(authorization.split(" ", 1)[1].strip())
    if not admin_id:
        raise HTTPException(status_code=401, detail="انتهت الجلسة. سجّل الدخول من جديد.")
    return admin_id


@router.post("/api/admin/login", summary="Sign in with the admin account stored in Postgres")
async def admin_login(body: AdminLogin) -> dict:
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(
            text("SELECT id, password_hash FROM admins WHERE lower(email) = lower(:email)"),
            {"email": body.email.strip()},
        )
        row = result.first()
    if row is None or not verify_password(body.password, row.password_hash):
        raise HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة.")
    return {"token": issue_admin_token(str(row.id)), "email": body.email.strip()}


@router.post("/api/reviews", summary="Submit a visitor review for admin approval")
async def create_review(body: ReviewCreate) -> dict:
    review_id = uuid4()
    role = body.role.strip() or "عميل"
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text(
                """
                INSERT INTO reviews (id, author_name, role, comment, rating, status)
                VALUES (:id, :name, :role, :comment, :rating, 'pending')
                """
            ),
            {
                "id": review_id,
                "name": body.name.strip(),
                "role": role,
                "comment": body.comment.strip(),
                "rating": body.rating,
            },
        )
        await session.commit()
        result = await session.execute(text("SELECT * FROM reviews WHERE id = :id"), {"id": review_id})
        row = result.one()
    return {"success": True, "review": _review(row)}


@router.get("/api/reviews/public", summary="Approved reviews shown on the contact page")
async def public_reviews() -> dict:
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(
            text("SELECT * FROM reviews WHERE status = 'accepted' ORDER BY created_at DESC"),
        )
        rows = result.all()
    return {"reviews": [_review(row) for row in rows]}


@router.get("/api/reviews", summary="All reviews for the admin dashboard")
async def list_reviews(authorization: str | None = Header(default=None)) -> dict:
    require_admin_token(authorization)
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(text("SELECT * FROM reviews ORDER BY created_at DESC"))
        rows = result.all()
    return {"reviews": [_review(row) for row in rows]}


@router.get("/api/reviews/stats", summary="Real review counts for the admin dashboard")
async def review_stats(authorization: str | None = Header(default=None)) -> dict:
    require_admin_token(authorization)
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'accepted') AS accepted,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'declined') AS declined,
                AVG(rating)::FLOAT AS average_rating
            FROM reviews
        """))
        row = result.one()
    return {
        "total": row.total,
        "accepted": row.accepted,
        "pending": row.pending,
        "declined": row.declined,
        "average_rating": row.average_rating,
    }


@router.patch("/api/reviews/{review_id}", summary="Approve or decline a review")
async def decide_review(
    review_id: UUID,
    body: ReviewDecision,
    authorization: str | None = Header(default=None),
) -> dict:
    require_admin_token(authorization)
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        result = await session.execute(
            text(
                """
                UPDATE reviews SET status = :status WHERE id = :id
                RETURNING *
                """
            ),
            {"status": body.status, "id": review_id},
        )
        row = result.first()
        await session.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="الرأي غير موجود.")
    return {"review": _review(row)}
