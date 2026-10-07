"""Async SQLAlchemy engine/session management."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def _async_url(url: str) -> tuple[str, dict]:
    """Accept libpq URLs from Neon (`postgresql://...?sslmode=require`) and SQLAlchemy async URLs."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", "")
    query.pop("channel_binding", None)
    scheme = "postgresql+asyncpg" if parts.scheme in {"postgresql", "postgres"} else parts.scheme
    cleaned = urlunsplit((scheme, parts.netloc, parts.path, "", ""))
    connect_args: dict = {}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = True
    if "pooler" in (parts.hostname or ""):
        connect_args["statement_cache_size"] = 0
    return cleaned, connect_args


@lru_cache
def get_engine() -> AsyncEngine:
    url, connect_args = _async_url(get_settings().database_url)
    return create_async_engine(
        url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=connect_args,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Services commit explicitly; uncommitted work is rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Session for Celery tasks. Each task runs in its own event loop (asyncio.run), and asyncpg
    connections are bound to a loop, so a dedicated non-pooled engine is created per task."""
    url, connect_args = _async_url(get_settings().database_url)
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
