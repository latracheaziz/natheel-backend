"""Redis-backed fixed-window rate limiting (with an in-memory backend for tests/dev)."""
from __future__ import annotations

import time
from collections import defaultdict
from functools import lru_cache
from typing import Callable

from fastapi import Request
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


class MemoryBackend:
    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))

    async def hit(self, key: str, window: int) -> tuple[int, int]:
        now = time.monotonic()
        count, expires = self._hits[key]
        if now >= expires:
            count, expires = 0, now + window
        count += 1
        self._hits[key] = (count, expires)
        return count, max(1, int(expires - now))

    def reset(self) -> None:
        self._hits.clear()


class RedisBackend:
    async def hit(self, key: str, window: int) -> tuple[int, int]:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
        return int(count), max(1, int(ttl))


_memory_backend = MemoryBackend()


def get_rate_limit_backend() -> MemoryBackend | RedisBackend:
    return _memory_backend if get_settings().rate_limit_backend == "memory" else RedisBackend()


def client_ip(request: Request) -> str:
    # Run uvicorn with --proxy-headers / --forwarded-allow-ips behind a trusted reverse proxy.
    return request.client.host if request.client else "unknown"


def parse_limit(spec: str) -> tuple[int, int]:
    count, window = spec.split("/")
    return int(count), int(window)


def rate_limit(scope: str, setting_name: str) -> Callable[[Request], object]:
    """Dependency factory. `setting_name` is the Settings attribute holding "<n>/<seconds>"."""

    async def dependency(request: Request) -> None:
        limit, window = parse_limit(getattr(get_settings(), setting_name))
        key = f"ratelimit:{scope}:{client_ip(request)}"
        try:
            count, retry_after = await get_rate_limit_backend().hit(key, window)
        except Exception as exc:  # Redis outage must not take the admin panel down.
            logger.warning("rate_limit_backend_unavailable", extra={"error_type": type(exc).__name__})
            return
        if count > limit:
            raise RateLimitedError("Too many requests. Please retry later.",
                                   headers={"Retry-After": str(retry_after)})

    return dependency


def reset_memory_rate_limits() -> None:
    _memory_backend.reset()
